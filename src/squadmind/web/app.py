"""Web arayüzü (Faz 3a: takım demo ekibi).

Akış: davet linki (?davet=<kod>) → FACEIT ile giriş (ayarlıysa; yoksa FACEIT nickname) →
maç geçmişi arka planda çekilir →
demolar tarayıcıdan parça parça yüklenir (tünel tek istekte ~100 MB sınırı koyar) →
işçi sırayla işler (aynı demo bir kez kuralı dahil) → oyuncu kendi raporunu ve takım
raporunu görür. Kullanıcıdan bilgi istenmez: kimlik FACEIT girişinden, gerisi Data API'den.
"""

from __future__ import annotations

import hmac
import html as html_lib
import logging
import re
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from pathlib import Path
from urllib.parse import quote

import markdown as md
import polars as pl
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from squadmind.faceit.client import FaceitAPIError, FaceitClient, FaceitNotFound
from squadmind.analysis import tactic_builder as tbld
from squadmind.analysis import team_builder as tb
from squadmind.ingest import demos as demo_rules
from squadmind.ingest.matches import profile_to_player_row, upsert_players
from squadmind.web.faceit_auth import FaceitAuthError, FaceitOAuth, pkce_pair
from squadmind.web.store import OPEN_STATES, QUEUED, UPLOADING, Store, WebUser
from squadmind.web.worker import Job, Worker

log = logging.getLogger("squadmind.web")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
COOKIE = "squadmind_oturum"
# FACEIT girişi sürerken (state + PKCE doğrulayıcı + davet) tutulan kısa ömürlü çerez
LOGIN_COOKIE = "squadmind_giris"
LOGIN_SECONDS = 600
# Yöneticinin ürettiği giriş linkinin ömrü (squadmind login-link)
LINK_SECONDS = 900
LINK_SALT = "squadmind-link"
# FACEIT ile girişte davetsiz kabul: takım sahibiyle aynı tarafta en az bu kadar maç
# (tek seferlik rastgele takım arkadaşı girmesin)
TEAM_MIN_SHARED = 3
# Bir takımda en fazla oyuncu (5 asıl + 2 yedek)
MAX_TEAM_MEMBERS = 7
_TEAM_ID = re.compile(r"[0-9a-f]{12}")
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
    oauth: FaceitOAuth | None = None            # FACEIT ile giriş (yoksa nickname ile katılım)


@lru_cache(maxsize=1)
def join_art() -> str:
    """Giriş sayfasının görseli: Dust2 tahtası (navmesh yerelde yoksa boş)."""
    try:
        from squadmind.viz.board import ARENA, Board, render_board
        return render_board(Board("de_dust2", target_places=[(1097, 2649)], target_radius=430),
                            width=560, pad=16, theme=ARENA)
    except Exception:  # noqa: BLE001 - görsel yoksa sayfa yine açılır
        log.warning("giriş sayfası görseli çizilemedi", exc_info=True)
        return ""


@lru_cache(maxsize=16)
def map_art(map_name: str) -> str:
    """Harita kartının görseli: navmesh'ten çizim (etiketsiz). Valve görseli kullanılmaz."""
    from squadmind.viz.board import ARENA, Board, render_board
    return render_board(Board(map_name), width=400, pad=6, theme=ARENA)


def safe_file_name(name: str) -> str:
    """Tarayıcıdan gelen dosya adını güvenli hale getirir (klasör kısmı atılır)."""
    base = Path(name.replace("\\", "/")).name
    return _SAFE_NAME.sub("_", base)[:200] or "demo.dem"


def create_app(settings: WebSettings, store: Store, worker: Worker,
               client_factory=FaceitClient) -> FastAPI:
    app = FastAPI(title="SquadMind", docs_url=None, redoc_url=None, openapi_url=None)
    signer = URLSafeTimedSerializer(settings.secret, salt="squadmind-oturum")
    login_signer = URLSafeTimedSerializer(settings.secret, salt="squadmind-giris")
    oauth = settings.oauth if settings.oauth and settings.oauth.configured else None
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
        ctx.setdefault("me", current_user(request))
        ctx.setdefault("owner", settings.team_owner)
        return TEMPLATES.TemplateResponse(request, name, ctx)

    # --- katılım --------------------------------------------------------------------

    def join_page(request: Request, davet: str = "", error: str | None = None) -> HTMLResponse:
        return page(request, "join.html", davet=davet, error=error,
                    closed=not settings.invite_code, faceit_login=oauth is not None,
                    invited=bool(davet), board=join_art())

    def invite_ok(davet: str) -> bool:
        return bool(settings.invite_code) and hmac.compare_digest(davet, settings.invite_code)

    def same_team_matches(faceit_id: str) -> int:
        """Oyuncunun takım sahibiyle aynı tarafta oynadığı maç sayısı (DB'deki skor tablolarından)."""
        if not settings.team_owner:
            return 0
        return store.run(lambda con: con.execute(
            """
            SELECT count(DISTINCT a.match_id)
            FROM match_player_stats a
            JOIN match_player_stats b ON b.match_id = a.match_id AND b.faction = a.faction
            JOIN players p ON p.faceit_id = b.player_id
            WHERE a.player_id = ? AND lower(p.nickname) = lower(?)
            """, [faceit_id, settings.team_owner]).fetchone()[0])

    def enter(request: Request, profile: dict, invited: bool, davet: str, verified: bool = False):
        """FACEIT profiliyle oturum aç.

        FACEIT girişinde (kimlik doğrulanmış) herkes girer ve kendi takımını kurabilir.
        Nickname ile katılımda kimlik doğrulanmadığı için davet şart. Takım sahibinin kadro
        sayfaları (takım raporu, taktik tahtası) için erişim: davet kodu, takım sahibinin kendisi
        ya da onunla aynı tarafta TEAM_MIN_SHARED maç.
        """
        row = profile_to_player_row(profile)
        if not row.get("steam_id"):
            return join_page(request, davet, "Bu FACEIT hesabına bağlı CS2 / Steam hesabı bulunamadı.")
        teammate = verified and (
            (settings.team_owner or "").lower() == (row["nickname"] or "").lower()
            or same_team_matches(row["faceit_id"]) >= TEAM_MIN_SHARED)
        if not verified and not invited and store.user(row["steam_id"]) is None:
            return join_page(request, davet, "Davet kodu geçersiz. Linki takım arkadaşından tekrar iste.")
        store.run(lambda con: upsert_players(con, [row]))
        store.add_user(row["steam_id"], row["faceit_id"], row["nickname"],
                       team_access=invited or teammate)
        worker.submit(Job("sync", row["steam_id"]))  # her girişte maç geçmişi tazelenir
        resp = RedirectResponse("/ben", status_code=303)
        resp.set_cookie(COOKIE, signer.dumps(row["steam_id"]), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
        resp.delete_cookie(LOGIN_COOKIE)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, davet: str = ""):
        if current_user(request):
            return RedirectResponse("/ben", status_code=303)
        return join_page(request, davet)

    @app.post("/katil", response_class=HTMLResponse)
    def join(request: Request, nickname: str = Form(...), davet: str = Form("")):
        # Nickname ile katılım her zaman davet kodu ister (kimlik doğrulanmaz); FACEIT girişi
        # açıkken de davet linkiyle gelen için yedek yol olarak durur
        if not invite_ok(davet):
            return join_page(request, davet, "Davet kodu geçersiz. Linki takım arkadaşından tekrar iste.")
        nickname = nickname.strip()
        try:
            with client_factory() as client:
                profile = client.get_player_by_nickname(nickname)
        except FaceitNotFound:
            return join_page(request, davet,
                             f"FACEIT'te '{nickname}' bulunamadı. Büyük / küçük harfe dikkat et.")
        except FaceitAPIError as exc:
            return join_page(request, davet, f"FACEIT'e ulaşılamadı: {exc}")
        return enter(request, profile, True, davet)

    @app.get("/giris/faceit")
    def faceit_login(request: Request, davet: str = ""):
        if oauth is None:
            raise HTTPException(404, "FACEIT ile giriş ayarlı değil.")
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        resp = RedirectResponse(oauth.authorize_url(state, challenge), status_code=303)
        resp.set_cookie(LOGIN_COOKIE, login_signer.dumps({"s": state, "v": verifier, "d": davet}),
                        max_age=LOGIN_SECONDS, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp

    @app.get("/giris/faceit/donus", response_class=HTMLResponse)
    def faceit_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if oauth is None:
            raise HTTPException(404, "FACEIT ile giriş ayarlı değil.")
        try:
            pending = login_signer.loads(request.cookies.get(LOGIN_COOKIE, ""), max_age=LOGIN_SECONDS)
        except BadSignature:
            return join_page(request, "", "Giriş süresi doldu; tekrar dene.")
        davet = str(pending.get("d", ""))
        if not code and not error and (oauth.flow == "implicit" or request.query_params.get("token")):
            # Anahtar adresin # kısmında: sunucuya gelmez, küçük sayfa okuyup POST eder
            return page(request, "faceit_return.html")
        if error or not code:
            return join_page(request, davet, "FACEIT girişi tamamlanmadı.")
        if not hmac.compare_digest(state, str(pending.get("s", ""))):
            return join_page(request, davet, "Giriş isteği doğrulanamadı; tekrar dene.")
        try:
            user = oauth.fetch_user(code, str(pending["v"]))
            with client_factory() as client:
                profile = client.get_player(user["guid"])
        except FaceitAuthError as exc:
            return join_page(request, davet, str(exc))
        except FaceitAPIError as exc:
            return join_page(request, davet, f"FACEIT profili alınamadı: {exc}")
        return enter(request, profile, invite_ok(davet), davet, verified=True)

    @app.post("/giris/faceit/anahtar", response_class=HTMLResponse)
    def faceit_token(request: Request, access_token: str = Form(""), state: str = Form(""),
                     error: str = Form("")):
        """Implicit akış: dönüş sayfası # kısmındaki anahtarı ve state'i buraya gönderir."""
        if oauth is None:
            raise HTTPException(404, "FACEIT ile giriş ayarlı değil.")
        try:
            pending = login_signer.loads(request.cookies.get(LOGIN_COOKIE, ""), max_age=LOGIN_SECONDS)
        except BadSignature:
            return join_page(request, "", "Giriş süresi doldu; tekrar dene.")
        davet = str(pending.get("d", ""))
        if error or not access_token:
            log.warning("FACEIT implicit dönüşü anahtarsız: %s", error[:200])
            detail = f": {error[:80]}" if error else "."
            return join_page(request, davet, f"FACEIT girişi tamamlanmadı{detail}")
        if not hmac.compare_digest(state, str(pending.get("s", ""))):
            return join_page(request, davet, "Giriş isteği doğrulanamadı; tekrar dene.")
        try:
            user = oauth.user_from_token(access_token)
            with client_factory() as client:
                profile = client.get_player(user["guid"])
        except FaceitAuthError as exc:
            return join_page(request, davet, str(exc))
        except FaceitAPIError as exc:
            return join_page(request, davet, f"FACEIT profili alınamadı: {exc}")
        return enter(request, profile, invite_ok(davet), davet, verified=True)

    # --- gizlilik / kullanım şartları (FACEIT onay ekranı bu adreslere bağlanır) ---------

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return page(request, "legal.html", page="privacy", title="Gizlilik")

    @app.get("/terms", response_class=HTMLResponse)
    def terms(request: Request):
        return page(request, "legal.html", page="terms", title="Kullanım şartları")

    @app.get("/giris/link")
    def login_link(request: Request, t: str = ""):
        """`squadmind login-link <nick>` ile üretilen imzalı link: kayıtlı kullanıcıya oturum açar.

        Link WEB_SECRET ile imzalı ve LINK_SECONDS geçerli; sadece siteye daha önce katılmış
        hesaplar için çalışır (yeni hesap açmaz, erişim yetkisi vermez).
        """
        try:
            steam_id = URLSafeTimedSerializer(settings.secret, salt=LINK_SALT).loads(
                t, max_age=LINK_SECONDS)
        except BadSignature:
            return join_page(request, "", "Giriş linkinin süresi dolmuş ya da geçersiz.")
        if store.user(steam_id) is None:
            return join_page(request, "", "Bu hesap siteye kayıtlı değil.")
        resp = RedirectResponse("/ben", status_code=303)
        resp.set_cookie(COOKIE, signer.dumps(steam_id), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return resp

    @app.get("/cikis")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # --- oyuncu sayfası ---------------------------------------------------------------

    def map_cards(user: WebUser) -> list[dict]:
        """Harita kartları: havuz + oyuncunun havuz dışında demosu olan haritalar."""
        counts = store.demo_counts(user.steam_id)
        maps = list(tb.MAP_POOL) + sorted(m for m in counts if m not in tb.MAP_POOL)
        return [{"map": m, "label": tb.map_label(m), "n": counts.get(m, 0),
                 "level": tb.demo_level(counts.get(m, 0)), "pool": m in tb.MAP_POOL}
                for m in maps]

    def busy(user: WebUser, uploads: list[dict]) -> bool:
        return any(u["status"] in OPEN_STATES for u in uploads) or             user.sync_status in ("bekliyor", "alınıyor")

    @app.get("/ben", response_class=HTMLResponse)
    def me(request: Request):
        """Genel bakış: toplam analiz sayıları, harita ilerlemesi, takımlar (yükleme yok)."""
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        uploads = store.uploads_of(user.steam_id)
        cards = map_cards(user)
        return page(request, "overview.html", user=user, cards=cards, totals=store.totals(user.steam_id),
                    ready=sum(c["level"] == "ok" for c in cards if c["pool"]), pool_size=len(tb.MAP_POOL),
                    min_demos=tb.MIN_DEMOS, teams=store.teams_of(user.faceit_id),
                    recent=uploads[:5], pending=busy(user, uploads))

    @app.get("/demolar", response_class=HTMLResponse)
    def demos(request: Request, harita: str = ""):
        """Demo yönetimi: harita kartları; her haritanın kendi yükleme alanı."""
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        uploads = store.uploads_of(user.steam_id)
        cards = map_cards(user)
        selected = harita if harita in tb.MAP_POOL else ""
        return page(request, "demos.html", user=user, uploads=uploads, cards=cards,
                    selected=selected, min_demos=tb.MIN_DEMOS, pending=busy(user, uploads),
                    chunk=CHUNK_BYTES, sizes=(demo_rules.MIN_DEMO_BYTES, demo_rules.MAX_DEMO_BYTES))

    @app.get("/harita/{map_name}.svg")
    def map_image(map_name: str):
        if map_name not in tb.MAP_POOL:
            raise HTTPException(404, "Harita yok.")
        return Response(map_art(map_name), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})

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
        # Her demo bir haritanın alanına yüklenir; demo o haritadan değilse işçi reddeder
        map_hint = str(body.get("map", ""))
        if map_hint not in tb.MAP_POOL:
            raise HTTPException(400, "Önce haritayı seç.")
        upload_id = store.new_upload(user.steam_id, name, size, map_hint)
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

    # --- bilgi sayfası (hata / boş durum mesajları) ------------------------------------

    def render_report(request: Request, title: str, markdown_text: str) -> HTMLResponse:
        # Raporda oyuncu adı gibi dışarıdan gelen metin var: ham HTML'e izin verme
        # (< > & kaçışlanır; Markdown işaretleri | * # etkilenmez)
        html = md.markdown(html_lib.escape(markdown_text, quote=False), extensions=["tables"])
        return page(request, "report.html", title=title, body=html)

    # --- arena: harita modeli, izinli oyuncular, canlı puanlar ---------------------------

    _builder_cache: dict[tuple, dict] = {}

    def builder_model(map_name: str) -> dict:
        """Haritanın düello modeli, bölge ızgarası ve tahta; demo sayısı değişince yenilenir."""
        from squadmind.analysis import duels as du
        from squadmind.analysis.maps import BOARD_PLACES
        from squadmind.analysis.player_metrics import load_demo_data
        from squadmind.viz.board import ARENA, Board, _Projection, map_polygons, render_board

        def build(con):
            n = con.execute("SELECT count(*) FROM matches WHERE demo_status = 'processed' AND map = ?",
                            [map_name]).fetchone()[0]
            key = (map_name, n)
            if key not in _builder_cache:
                d = load_demo_data(con, map_name)
                width, pad = 640, 16
                proj = _Projection(map_polygons(map_name), width, pad)
                _builder_cache.clear()
                duel_df = du.duels(d, map_name)
                _builder_cache[key] = {
                    "duels": duel_df, "match_ids": d.match_maps.filter(
                        pl.col("map") == map_name)["match_id"].to_list(),
                    "model": du.strengths(duel_df), "grid": tbld.place_grid(d),
                    "board": render_board(Board(map_name, places=BOARD_PLACES.get(map_name, {}),
                                                target_places=[]), width=width, pad=pad, theme=ARENA),
                    "proj": {"minx": proj.minx, "maxy": proj.maxy, "s": proj.s, "pad": proj.pad,
                             "w": width, "h": proj.height},
                }
            return _builder_cache[key]
        return store.run(build, lock=False)

    # feats: stat ekli (κ=0,25), feats0: yalnız ELO (κ=0; profesör kararı: ana olasılık bu),
    # weapon: silah etkisi (opsiyonel, kullanıcı silah seçerse)
    _rating_cache: dict[str, Any] = {"key": None, "ctx": None, "feats": {}, "feats0": {}, "at": {},
                                     "weapon": None}

    def fit_weapons(duel_df, roster: list[dict], ctx):
        """Silah etkisi (rating.fit_weapon_effect; profesör onayı: opsiyonel, Tüfek–Tüfek = kapalı).

        Ofset = silahsız modelin T lehine logit'i; düellodaki oyuncuların o maçtaki FACEIT
        seviyesinin ELO karşılığıyla (eğitimdeki ELO da seviye ortası; birleşik ELO ile korelasyon
        0,97). Seviyesi bilinmeyen oyuncunun düellosu atlanır.
        """
        from squadmind.analysis import rating as rt
        lv = {(r["match_id"], r["steam_id"]): rt.level_elo(r["level"])[0] for r in roster}
        beta = float(ctx.model.beta[0]) if len(ctx.model.beta) else 0.0
        cls_t, cls_ct, dist, won, off = [], [], [], [], []
        for r in duel_df.iter_rows(named=True):
            t_is_winner = r["winner_side"] == "T"
            t, ct = (r["winner"], r["loser"]) if t_is_winner else (r["loser"], r["winner"])
            et, ec = lv.get((r["match_id"], t)), lv.get((r["match_id"], ct))
            if et is None or ec is None or r.get("winner_cls") is None:
                continue
            cls_t.append(r["winner_cls"] if t_is_winner else r["loser_cls"])
            cls_ct.append(r["loser_cls"] if t_is_winner else r["winner_cls"])
            dist.append(r["dist"]); won.append(1.0 if t_is_winner else 0.0)
            off.append(beta * (et - ec) / rt.ELO_UNIT)
        return rt.fit_weapon_effect(cls_t, cls_ct, dist, won, off)

    def builder_ratings(map_name: str, players: list[dict]) -> dict[str, dict]:
        """Doğrulanmış güç puanı (analysis/rating.py): ortalama rakibe karşı beklenen düello
        yüzdesi, %90 aralık, birleşik ELO. Model demo sayısı değişince yeniden kurulur; oyuncu
        özellikleri FACEIT'ten (önbellekli) gelir. FACEIT'e ulaşılamazsa boş döner (sayfa
        puansız açılır)."""
        from squadmind.analysis import rating as rt

        bm = builder_model(map_name)
        key = (map_name, len(bm["match_ids"]))
        try:
            with client_factory() as client:
                if _rating_cache["key"] != key:
                    roster = []
                    for mid in bm["match_ids"]:
                        det = client.get_match_details(mid)
                        for team in (det.get("teams") or {}).values():
                            for p in team.get("roster") or []:
                                roster.append({"match_id": mid, "steam_id": p.get("game_player_id"),
                                               "faceit_id": p["player_id"],
                                               "level": p.get("game_skill_level"),
                                               "started_at": det["started_at"],
                                               "finished_at": det["finished_at"]})
                    ctx = rt.build_context(bm["duels"], client, roster)
                    _rating_cache.update(key=key, ctx=ctx, feats={}, feats0={},
                                         weapon=fit_weapons(bm["duels"], roster, ctx))
                ctx, feats, at = _rating_cache["ctx"], _rating_cache["feats"], _rating_cache["at"]
                now = time.time()
                for p in players:
                    if p["steam_id"] not in feats or now - at.get(p["steam_id"], 0) > 60:
                        try:
                            prof = client.get_player_live(p["faceit_id"])   # ELO her zaman FACEIT'ten
                            game = (prof.get("games") or {}).get("cs2") or {}
                            hist = rt.summarize_history(client.get_player_match_stats(p["faceit_id"]))
                            elo = rt._num(game.get("faceit_elo"))
                            kw = {"elo": elo if elo and elo > 0 else None, "level": game.get("skill_level")}
                            feats[p["steam_id"]] = rt.player_features(hist, ctx.pop, **kw)
                            _rating_cache["feats0"][p["steam_id"]] = rt.player_features(
                                hist, ctx.pop, kappa=0.0, **kw)
                            at[p["steam_id"]] = now   # sadece başarıda: hata olunca bir sonraki istekte yeniden dene
                        except Exception:  # noqa: BLE001 - tek oyuncu başarısız olursa diğerleri puanlı kalsın
                            log.exception("oyuncu puanı alınamadı: %s", p.get("nickname"))
        except Exception:  # noqa: BLE001 - FACEIT yoksa puansız devam
            log.exception("güç puanı kurulamadı")
            return {}
        out = {}
        for p in players:
            f = feats.get(p["steam_id"])
            if f is None:
                continue
            score, lo, hi = rt.rating_score(ctx.model, f, p["steam_id"])
            # Canlı ELO alınamayıp seviye ortası kullanıldıysa "elo" gösterilmez, güven "az veri"
            few = (not f.elo_exact) or f.n_matches < rt.MIN_MATCHES
            out[p["steam_id"]] = {
                "score": round(score, 1), "lo": round(lo, 1), "hi": round(hi, 1),
                "elo": round(f.elo) if f.elo_exact else None, "combined": round(f.combined_elo),
                "sd": round(f.combined_sd), "matches": f.n_matches,
                "conf": "az veri" if few else ("belirgin" if lo > 50 or hi < 50 else "belirsiz"),
            }
        return out

    def builder_players(user: WebUser, map_name: str) -> list[dict]:
        """Taktikte kullanılabilecek oyuncular (izin modeli): kendisi, takımlarında daveti kabul
        etmiş üyeler ve kadro erişimi varsa takım sahibinin çekirdek kadrosu. Öneri sırası:
        en çok aynı tarafta oynananlar önce."""
        def load(con):
            ids = {user.faceit_id}
            for t in store.teams_of(user.faceit_id):
                if t["status"] == "üye":
                    ids |= {m["faceit_id"] for m in store.team_members(t["id"]) if m["status"] == "üye"}
            if user.team_access and settings.team_owner:
                owner = con.execute("SELECT faceit_id FROM players WHERE lower(nickname) = lower(?)",
                                    [settings.team_owner]).fetchone()
                if owner:
                    ids.add(owner[0])
                    ids |= {r["faceit_id"] for r in tb.played_with(con, owner[0], limit=4, min_together=10)}
            rows = con.execute(
                """
                SELECT p.faceit_id, p.steam_id, p.nickname, p.level,
                       count(DISTINCT m.match_id) FILTER (WHERE m.demo_status = 'processed' AND m.map = ?) AS demos
                FROM players p LEFT JOIN match_player_stats s ON s.player_id = p.faceit_id
                LEFT JOIN matches m ON m.match_id = s.match_id
                WHERE list_contains(?, p.faceit_id) AND p.steam_id IS NOT NULL
                GROUP BY 1, 2, 3, 4
                """, [map_name, list(ids)]).fetchall()
            rank = {r["faceit_id"]: i for i, r in enumerate(tb.played_with(con, user.faceit_id, limit=50, min_together=1))}
            out = [{"faceit_id": f, "steam_id": s, "nickname": n, "level": lv, "demos": dm,
                    "me": f == user.faceit_id, "rank": -1 if f == user.faceit_id else rank.get(f, 999)}
                   for f, s, n, lv, dm in rows]
            return sorted(out, key=lambda r: (r["rank"], (r["nickname"] or "").lower()))
        return store.run(load, lock=False)

    # --- düello arenası ------------------------------------------------------------------

    def level_options() -> list[dict]:
        """Rakip seçimi: her FACEIT seviyesinde ortalama rakip (aralığın ortası ELO)."""
        from squadmind.analysis import rating as rt
        # Seviye 10 açık uçlu: aralık ortası (2200) yerine bu lobilerde ölçülen ortalama 2300
        # (535 takımın maç ELO'sundan; profesör kararı, yalnız arenadaki ortalama rakip için)
        return [{"level": lv, "elo": 2300 if lv == 10 else round((lo + hi) / 2),
                 "range": "2001+" if lv == 10 else f"{int(lo)}-{int(hi)}"}
                for lv, (lo, hi) in rt.LEVEL_ELO_RANGE.items()]

    @app.get("/arena", response_class=HTMLResponse)
    def arena_page(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        map_name = "de_dust2"
        try:
            bm = builder_model(map_name)
        except Exception:  # noqa: BLE001
            log.exception("arena modeli kurulamadı")
            return render_report(request, "Arena", "_Bu harita için henüz yeterli veri yok._")
        players = builder_players(user, map_name)
        payload = {"map": map_name, "proj": bm["proj"], "grid": bm["grid"], "players": players,
                   "ratings": builder_ratings(map_name, players), "levels": level_options(),
                   "me": user.steam_id}
        return page(request, "arena.html", user=user, board=bm["board"], payload=payload,
                    w=bm["proj"]["w"], h=bm["proj"]["h"])

    @app.post("/api/arena/duel")
    async def arena_duel(request: Request):
        """İki yerleşim → görüş, mesafe, hat ve (görüyorlarsa) A'nın kazanma olasılığı.

        Gövde: {a: {steam_id, x, y}, b: {steam_id | elo, x, y}, side_a: "T" | "CT"}.
        A izinli oyunculardan biri olmalı; B ya izinli bir oyuncu ya da bir ELO (ortalama rakip).
        """
        from squadmind.analysis import duels as du
        from squadmind.analysis import rating as rt
        from squadmind.analysis.geometry import can_see_xy

        user = require_user(request)
        body = await request.json()
        map_name = "de_dust2"
        bm = builder_model(map_name)
        players = builder_players(user, map_name)
        allowed = {p["steam_id"] for p in players}
        a, b = body.get("a") or {}, body.get("b") or {}
        side_a = body.get("side_a") if body.get("side_a") in ("T", "CT") else "T"
        if a.get("steam_id") not in allowed or (b.get("steam_id") and b["steam_id"] not in allowed):
            raise HTTPException(403, "Bu oyuncunun verisini kullanma iznin yok.")
        try:
            ax, ay, bx, by = (float(a["x"]), float(a["y"]), float(b["x"]), float(b["y"]))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Konum eksik.") from None
        pa, za = tbld.place_at(bm["grid"], ax, ay)
        pb, zb = tbld.place_at(bm["grid"], bx, by)
        if pa is None or pb is None:
            return {"visible": None, "reason": "Nokta oyuncuların gezdiği alanın dışında."}
        dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
        # Yükseklik navmesh katlarından (hücre z'si yalnızca yedek): yanlış "görmüyor" azalır
        visible = can_see_xy(map_name, (ax, ay), (bx, by), za, zb)
        line = du.line_of(map_name, pa, pb)
        out = {"visible": visible, "place_a": pa, "place_b": pb, "dist": round(dist),
               "band": du.band_of(dist), "line": tbld.line_names(map_name).get(line or "", None)}
        if not visible:
            out["reason"] = "Birbirlerini görmüyorlar: düello yok."
            return out
        ratings = builder_ratings(map_name, players)
        ctx = _rating_cache["ctx"]
        fa, fa0 = _rating_cache["feats"].get(a["steam_id"]), _rating_cache["feats0"].get(a["steam_id"])
        if ctx is None or fa is None or fa0 is None:
            raise HTTPException(503, "Güç puanı şu an hesaplanamadı (FACEIT).")
        if b.get("steam_id"):
            fb, fb0, b_key = (_rating_cache["feats"].get(b["steam_id"]),
                              _rating_cache["feats0"].get(b["steam_id"]), b["steam_id"])
        else:
            elo = max(100.0, min(4000.0, float(b.get("elo") or 1000)))
            # Ortalama rakip: boş geçmiş → o ELO'nun beklentisi; stat eki 0 (iki model aynı)
            fb = fb0 = rt.player_features(rt.History((), ()), ctx.pop, elo=elo)
            b_key = "ortalama"
        if fb is None or fb0 is None:
            raise HTTPException(503, "Rakibin puanı hesaplanamadı.")
        # Silah (profesör kararı: opsiyonel; Tüfek–Tüfek = etki yok)
        wa, wb = body.get("weapon_a", "rifle"), body.get("weapon_b", "rifle")
        wkw = {}
        if wa in ("pistol", "rifle", "awp") and wb in ("pistol", "rifle", "awp") and (wa, wb) != ("rifle", "rifle") \
                and _rating_cache["weapon"] is not None:
            wkw = {"weapon": _rating_cache["weapon"], "cls_a": wa, "cls_b": wb, "dist": dist}
        # Ana olasılık: yalnız canlı ELO (κ=0). Stat düzeltmeli olasılık ayrı satır (kanıtı zayıf)
        m0 = rt.matchup(ctx.model, a["steam_id"], side_a, b_key, line, fa0, fb0, **wkw)
        m = rt.matchup(ctx.model, a["steam_id"], side_a, b_key, line, fa, fb, **wkw)
        conf = m0.conf
        if b_key == "ortalama":
            # Ortalama rakip bir tanım (verilen ELO'da popülasyon ortalaması), eksik veri değil:
            # güven yalnızca A'nın verisine ve aralığın %50'yi dışlayıp dışlamadığına bakar
            few = (not fa0.elo_exact) or fa0.n_matches < rt.MIN_MATCHES
            conf = "az veri" if few else ("belirgin" if m0.lo > 0.5 or m0.hi < 0.5 else "belirsiz")
        gap = fa0.combined_elo - fb0.combined_elo
        out.update({"p": round(m0.p, 3), "lo": round(m0.lo, 3), "hi": round(m0.hi, 3), "conf": conf,
                    "p_stat": round(m.p, 3), "a_elo": round(fa0.combined_elo),
                    "b_elo": round(fb0.combined_elo), "gap": round(gap),
                    "big_gap": abs(gap) > 800, "weapon": bool(wkw),
                    "a_combined": round(fa.combined_elo), "b_combined": round(fb.combined_elo),
                    "a_score": ratings.get(a["steam_id"], {}).get("score")})
        return out

    # --- takım oluşturma ---------------------------------------------------------------

    def member_of(team_id: str, user: WebUser) -> tuple[dict, dict | None]:
        """Takım + kullanıcının üyelik kaydı; takım yoksa ya da kullanıcı listede yoksa 404."""
        team = store.team(team_id) if _TEAM_ID.fullmatch(team_id) else None
        if team is None:
            raise HTTPException(404, "Takım bulunamadı.")
        mine = next((m for m in store.team_members(team_id) if m["faceit_id"] == user.faceit_id), None)
        if mine is None:
            raise HTTPException(404, "Takım bulunamadı.")
        return team, mine

    @app.get("/takimlar", response_class=HTMLResponse)
    def teams(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        return page(request, "teams.html", user=user, teams=store.teams_of(user.faceit_id))

    @app.post("/takimlar")
    def create_team(request: Request, name: str = Form(...)):
        user = require_user(request)
        name = name.strip()[:40] or f"{user.nickname} takımı"
        return RedirectResponse(f"/takimlar/{store.create_team(name, user)}", status_code=303)

    @app.get("/takimlar/{team_id}", response_class=HTMLResponse)
    def team_page(request: Request, team_id: str, q: str = ""):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        team, mine = member_of(team_id, user)
        members = store.team_members(team_id)
        ids = [m["faceit_id"] for m in members]
        # Paylaşım izni: sadece daveti kabul etmiş üyelerin verisi gösterilir
        shared = [m["faceit_id"] for m in members if m["status"] == "üye"]
        is_owner = team["owner_steam_id"] == user.steam_id

        def load(con):
            out = {"stats": tb.member_stats(con, shared), "pairs": tb.pair_matrix(con, shared),
                   "maps": tb.team_maps(con, shared), "demos": tb.demo_counts(con, shared),
                   "suggest": tb.played_with(con, user.faceit_id, exclude=ids),
                   "found": tb.search_players(con, q) if q else []}
            return out
        data = store.run(load, lock=False)
        names = {m["faceit_id"]: m["nickname"] for m in members}
        maps = list(tb.MAP_POOL) + sorted({mp for d in data["demos"].values() for mp in d
                                           if mp not in tb.MAP_POOL})
        readiness = [{"map": mp, "label": tb.map_label(mp),
                      "per": [(names[f], data["demos"][f].get(mp, 0),
                               tb.demo_level(data["demos"][f].get(mp, 0))) for f in shared],
                      "ready": sum(data["demos"][f].get(mp, 0) >= tb.MIN_DEMOS for f in shared)}
                     for mp in maps]
        return page(request, "team.html", user=user, team=team, mine=mine, members=members,
                    is_owner=is_owner, shared=shared, names=names, q=q, readiness=readiness,
                    min_demos=tb.MIN_DEMOS, max_members=MAX_TEAM_MEMBERS, map_label=tb.map_label,
                    full=len(members) >= MAX_TEAM_MEMBERS, **data)

    @app.post("/takimlar/{team_id}/ekle")
    def add_member(request: Request, team_id: str, faceit_id: str = Form(""),
                   nickname: str = Form("")):
        user = require_user(request)
        team, mine = member_of(team_id, user)
        if mine["status"] != "üye":
            raise HTTPException(403, "Önce daveti kabul et.")
        if len(store.team_members(team_id)) >= MAX_TEAM_MEMBERS:
            raise HTTPException(409, f"Bir takımda en fazla {MAX_TEAM_MEMBERS} oyuncu olabilir.")
        nickname = nickname.strip()
        if not faceit_id:
            # DB'de olmayan oyuncu: FACEIT'te tam nickname ile bul, profilini kaydet
            try:
                with client_factory() as client:
                    profile = client.get_player_by_nickname(nickname)
            except FaceitNotFound:
                return RedirectResponse(f"/takimlar/{team_id}?q={quote(nickname)}&yok=1", status_code=303)
            row = profile_to_player_row(profile)
            store.run(lambda con: upsert_players(con, [row]))
            faceit_id, nickname = row["faceit_id"], row["nickname"]
        store.add_member(team_id, faceit_id, nickname or faceit_id, user.steam_id)
        return RedirectResponse(f"/takimlar/{team_id}", status_code=303)

    @app.post("/takimlar/{team_id}/kabul")
    def accept(request: Request, team_id: str):
        user = require_user(request)
        member_of(team_id, user)
        store.set_member_status(team_id, user.faceit_id, "üye")
        return RedirectResponse(f"/takimlar/{team_id}", status_code=303)

    @app.post("/takimlar/{team_id}/cikar")
    def remove(request: Request, team_id: str, faceit_id: str = Form(...)):
        user = require_user(request)
        team, _ = member_of(team_id, user)
        is_owner = team["owner_steam_id"] == user.steam_id
        # Sahip herkesi çıkarabilir (kendisi hariç); üye sadece kendini (daveti reddetmek dahil)
        if (is_owner and faceit_id == user.faceit_id) or (not is_owner and faceit_id != user.faceit_id):
            raise HTTPException(403, "Bu oyuncuyu çıkaramazsın.")
        store.remove_member(team_id, faceit_id)
        return RedirectResponse("/takimlar" if faceit_id == user.faceit_id else f"/takimlar/{team_id}",
                                status_code=303)

    return app


def make_login_link(secret: str, base_url: str, steam_id: str) -> str:
    """Yönetici giriş linki (bkz. /giris/link)."""
    token = URLSafeTimedSerializer(secret, salt=LINK_SALT).dumps(steam_id)
    return f"{base_url.rstrip('/')}/giris/link?t={token}"


def default_secret(secret: str) -> str:
    """Ayarlı gizli anahtar; yoksa rastgele (sunucu yeniden başlayınca oturumlar düşer)."""
    if secret:
        return secret
    log.warning("WEB_SECRET yok: rastgele anahtar kullanılıyor, yeniden başlatınca oturumlar düşer")
    return secrets.token_urlsafe(32)
