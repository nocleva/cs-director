"""Web arayüzü (Faz 3a: takım demo ekibi).

Akış: davet linki (?davet=<kod>) → FACEIT nickname → maç geçmişi arka planda çekilir →
demolar tarayıcıdan parça parça yüklenir (tünel tek istekte ~100 MB sınırı koyar) →
işçi sırayla işler (aynı demo bir kez kuralı dahil) → oyuncu kendi raporunu ve takım
raporunu görür. Sadece kullanıcı bilgisi istenir: FACEIT nickname.
"""

from __future__ import annotations

import hmac
import html as html_lib
import logging
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

import markdown as md
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from squadmind.faceit.client import FaceitAPIError, FaceitClient, FaceitNotFound
from squadmind.ingest import demos as demo_rules
from squadmind.ingest.matches import profile_to_player_row, upsert_players
from squadmind.reports.player_report import build_player_report
from squadmind.reports.team_report import build_team_report
from squadmind.viz import tactic as tactic_view
from squadmind.web.store import OPEN_STATES, QUEUED, UPLOADING, Store, WebUser
from squadmind.web.worker import Job, Worker

log = logging.getLogger("squadmind.web")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
COOKIE = "squadmind_oturum"
SESSION_DAYS = 30
# Parça boyutu: Cloudflare ücretsiz tüneli tek istekte ~100 MB'a izin verir
CHUNK_BYTES = 32 * 1024 * 1024
# Bir oyuncunun aynı anda açık (yüklenen / sırada / işlenen) en fazla demo sayısı
MAX_OPEN_UPLOADS = 5
# Dosya adı: FACEIT adı (maç ID'si) korunsun, gerisi güvenli karakterlere indirgensin
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class WebSettings:
    invite_code: str
    secret: str
    required_demos: int
    team_owner: str | None
    map_name: str | None
    uploads_dir: Path
    strats_dir: Path = tactic_view.STRATS_DIR  # taktik tahtasının strat dosyaları


def safe_file_name(name: str) -> str:
    """Tarayıcıdan gelen dosya adını güvenli hale getirir (klasör kısmı atılır)."""
    base = Path(name.replace("\\", "/")).name
    return _SAFE_NAME.sub("_", base)[:200] or "demo.dem"


def create_app(settings: WebSettings, store: Store, worker: Worker,
               client_factory=FaceitClient) -> FastAPI:
    app = FastAPI(title="SquadMind", docs_url=None, redoc_url=None, openapi_url=None)
    signer = URLSafeTimedSerializer(settings.secret, salt="squadmind-oturum")
    store.map_name = settings.map_name

    # --- oturum ---------------------------------------------------------------------

    def current_user(request: Request) -> WebUser | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        try:
            steam_id = signer.loads(token, max_age=SESSION_DAYS * 86400)
        except BadSignature:
            return None
        return store.user(steam_id)

    def require_user(request: Request) -> WebUser:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "Oturum yok; davet linkiyle katıl.")
        return user

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, ctx)

    # --- katılım --------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, davet: str = ""):
        if current_user(request):
            return RedirectResponse("/ben", status_code=303)
        return page(request, "join.html", davet=davet, error=None,
                    closed=not settings.invite_code)

    @app.post("/katil", response_class=HTMLResponse)
    def join(request: Request, nickname: str = Form(...), davet: str = Form("")):
        def fail(msg: str):
            return page(request, "join.html", davet=davet, error=msg,
                        closed=not settings.invite_code)

        if not settings.invite_code or not hmac.compare_digest(davet, settings.invite_code):
            return fail("Davet kodu geçersiz. Linki takım arkadaşından tekrar iste.")
        nickname = nickname.strip()
        try:
            with client_factory() as client:
                profile = client.get_player_by_nickname(nickname)
        except FaceitNotFound:
            return fail(f"FACEIT'te '{nickname}' bulunamadı. Büyük / küçük harfe dikkat et.")
        except FaceitAPIError as exc:
            return fail(f"FACEIT'e ulaşılamadı: {exc}")
        row = profile_to_player_row(profile)
        if not row.get("steam_id"):
            return fail("Bu FACEIT hesabına bağlı CS2 / Steam hesabı bulunamadı.")
        store.run(lambda con: upsert_players(con, [row]))
        store.add_user(row["steam_id"], row["faceit_id"], row["nickname"])
        worker.submit(Job("sync", row["steam_id"]))
        resp = RedirectResponse("/ben", status_code=303)
        resp.set_cookie(COOKIE, signer.dumps(row["steam_id"]), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return resp

    @app.get("/cikis")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # --- oyuncu sayfası ---------------------------------------------------------------

    @app.get("/ben", response_class=HTMLResponse)
    def me(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        uploads = store.uploads_of(user.steam_id)
        done = store.done_count(user.steam_id)
        pending = any(u["status"] in OPEN_STATES for u in uploads) or \
            user.sync_status in ("bekliyor", "alınıyor")
        return page(request, "me.html", user=user, uploads=uploads, done=done,
                    required=settings.required_demos, pending=pending,
                    map_name=(settings.map_name or "").removeprefix("de_"),
                    chunk=CHUNK_BYTES, team_owner=settings.team_owner,
                    sizes=(demo_rules.MIN_DEMO_BYTES, demo_rules.MAX_DEMO_BYTES))

    # --- parça parça yükleme ------------------------------------------------------------

    @app.post("/api/yukleme")
    async def upload_start(request: Request):
        user = require_user(request)
        body = await request.json()
        name, size = safe_file_name(str(body.get("name", ""))), int(body.get("size", 0))
        if not name.lower().endswith(demo_rules.DEMO_SUFFIXES):
            raise HTTPException(400, "Sadece .dem, .dem.zst ya da .dem.gz yüklenebilir.")
        if not demo_rules.MIN_DEMO_BYTES <= size <= demo_rules.MAX_DEMO_BYTES:
            raise HTTPException(400, f"Dosya boyutu demo için şüpheli ({size / 1048576:.1f} MB).")
        if store.open_uploads(user.steam_id) >= MAX_OPEN_UPLOADS:
            raise HTTPException(429, "Önceki yüklemelerin işlenmesini bekle.")
        upload_id = store.new_upload(user.steam_id, name, size)
        folder = settings.uploads_dir / upload_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).touch()
        return {"id": upload_id, "chunk": CHUNK_BYTES}

    def _owned_upload(user: WebUser, upload_id: str) -> dict:
        up = store.upload(upload_id) if re.fullmatch(r"[0-9a-f]{32}", upload_id) else None
        if up is None or up["steam_id"] != user.steam_id:
            raise HTTPException(404, "Yükleme bulunamadı.")
        return up

    @app.put("/api/yukleme/{upload_id}/{index}")
    async def upload_chunk(request: Request, upload_id: str, index: int):
        user = require_user(request)
        up = _owned_upload(user, upload_id)
        if up["status"] != UPLOADING:
            raise HTTPException(409, "Bu yükleme kapandı.")
        data = await request.body()
        offset = index * CHUNK_BYTES
        if index < 0 or len(data) > CHUNK_BYTES or offset + len(data) > up["size_bytes"]:
            raise HTTPException(400, "Parça sınır dışında.")
        path = settings.uploads_dir / upload_id / up["file_name"]
        with path.open("r+b") as f:
            f.seek(offset)
            f.write(data)
        return {"ok": True}

    @app.post("/api/yukleme/{upload_id}/bitti")
    def upload_complete(request: Request, upload_id: str):
        user = require_user(request)
        up = _owned_upload(user, upload_id)
        path = settings.uploads_dir / upload_id / up["file_name"]
        actual = path.stat().st_size if path.exists() else -1
        if actual != up["size_bytes"]:
            # Eksik / fazla parça: dosya bildirilen boyutta değil → işlenmez
            store.set_upload(upload_id, "hata", f"eksik yükleme ({actual} / {up['size_bytes']} bayt)")
            path.unlink(missing_ok=True)
            raise HTTPException(400, "Yükleme eksik; tekrar dene.")
        store.set_upload(upload_id, QUEUED)
        worker.submit(Job("ingest", upload_id, path))
        return {"ok": True}

    @app.get("/api/durum")
    def status(request: Request):
        user = require_user(request)
        return JSONResponse({"sync": user.sync_status,
                             "uploads": [{k: u[k] for k in ("id", "file_name", "status", "message")}
                                         for u in store.uploads_of(user.steam_id)]})

    # --- raporlar ----------------------------------------------------------------------

    def render_report(request: Request, title: str, markdown_text: str) -> HTMLResponse:
        # Raporda oyuncu adı gibi dışarıdan gelen metin var: ham HTML'e izin verme
        # (< > & kaçışlanır; Markdown işaretleri | * # etkilenmez)
        html = md.markdown(html_lib.escape(markdown_text, quote=False), extensions=["tables"])
        return page(request, "report.html", title=title, body=html)

    @app.get("/rapor/ben", response_class=HTMLResponse)
    def my_report(request: Request):
        user = require_user(request)
        try:
            rep = store.run(lambda con: build_player_report(con, user.nickname, settings.map_name),
                            lock=False)
        except LookupError as exc:
            return render_report(request, "Oyuncu raporu", f"_{exc}_")
        return render_report(request, f"Oyuncu raporu: {user.nickname}", rep.markdown)

    @app.get("/rapor/takim", response_class=HTMLResponse)
    def team_report(request: Request):
        user = require_user(request)
        owner = settings.team_owner or user.nickname
        try:
            rep = store.run(lambda con: build_team_report(con, owner, settings.map_name),
                            lock=False)
        except LookupError as exc:
            return render_report(request, "Takım raporu", f"_{exc}_")
        return render_report(request, "Takım raporu", rep.markdown)

    # --- taktik tahtası -------------------------------------------------------------------

    @app.get("/taktik", response_class=HTMLResponse)
    def tactic(request: Request, r: str = ""):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        strats = tactic_view.load_strats(settings.strats_dir, settings.map_name)
        found = tactic_view.find_tab(strats, r)
        if found is None:
            return render_report(request, "Taktik tahtası", "_Bu harita için henüz strat yok._")
        strat, tab = found
        owner = settings.team_owner or user.nickname
        try:
            ctx = store.run(lambda con: tactic_view.tactic_page(con, owner, strat), lock=False)
        except LookupError as exc:
            return render_report(request, "Taktik tahtası", f"_{exc}_")
        entry = int(strat.raw.get("entry", 0))
        return page(request, "tactic.html", user=user, strat=strat, tab=tab,
                    tabs=[(s, t) for s in strats for t in s.tabs],
                    map_label=strat.map_name.removeprefix("de_").capitalize(),
                    stat=tactic_view.stat_line(tab, ctx["stats"], strat.side),
                    entry_label=f"{entry // 60}:{entry % 60:02d}",
                    spot_window=tactic_view.SPOT_WINDOW, **ctx)

    return app


def default_secret(secret: str) -> str:
    """Ayarlı gizli anahtar; yoksa rastgele (sunucu yeniden başlayınca oturumlar düşer)."""
    if secret:
        return secret
    log.warning("WEB_SECRET yok: rastgele anahtar kullanılıyor, yeniden başlatınca oturumlar düşer")
    return secrets.token_urlsafe(32)
