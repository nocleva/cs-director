"""Web arayüzü (Faz 3a: takım demo ekibi).

Akış: davet linki (?davet=<kod>) → FACEIT ile giriş (ayarlıysa; yoksa FACEIT nickname) →
maç geçmişi arka planda çekilir →
demolar tarayıcıdan parça parça yüklenir (tünel tek istekte ~100 MB sınırı koyar) →
işçi sırayla işler (aynı demo bir kez kuralı dahil) → oyuncu kendi raporunu ve takım
raporunu görür. Kullanıcıdan bilgi istenmez: kimlik FACEIT girişinden, gerisi Data API'den.
"""

from __future__ import annotations

import collections
import hmac
import json
import mimetypes
import html as html_lib
import logging
import re
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from pathlib import Path
from urllib.parse import quote, urlsplit

import markdown as md
import polars as pl
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from squadmind.faceit.client import FaceitAPIError, FaceitClient, FaceitNotFound
from squadmind.analysis import tactic_builder as tbld
from squadmind.analysis import team_builder as tb
from squadmind.ingest import demos as demo_rules
from squadmind.ingest.matches import profile_to_player_row, upsert_players
from squadmind.web import accounts as acc
from squadmind.web import faceit_data as fdata
from squadmind.web.faceit_auth import FaceitAuthError, FaceitOAuth, pkce_pair
from squadmind.web.mailer import MailError, Mailer
from squadmind.web.store import OPEN_STATES, QUEUED, UPLOADING, Store, WebUser
from squadmind.web.worker import Job, Worker, demo_resource

log = logging.getLogger("squadmind.web")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
COOKIE = "squadmind_oturum"
ERROR_COOKIE = "squadmind_giris_hata"  # açılır pencereden ana sayfaya taşınan giriş hatası
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
# Spot önerisi sıralaması bu kadar işlenmiş Dust2 maçına kadar KAPALI (kullanıcı kararı 24.10)
SPOT_MIN_MATCHES = 40
# CSRF: çerezdeki rastgele değer + forma konan imzalı kopyası (çift gönderim)
CSRF_COOKIE = "squadmind_csrf"
CSRF_SECONDS = 12 * 3600
# "E-posta ekle" ekranını bir süre erteleyen çerez (eski oturumlar için)
SKIP_COOKIE = "squadmind_eposta_atla"
VERIFY_SECONDS = 24 * 3600   # e-posta doğrulama bağlantısı
RESET_SECONDS = 3600         # şifre sıfırlama bağlantısı
# "FACEIT'ten getir": kullanıcı başı dakikada en fazla bu kadar imzalı link / sunucu işi isteği
FACEIT_RATE_PER_MIN = 10
# DEMO_READY olay adları (FACEIT belgesi: match_demo_ready; DEMO_READY de kabul edilir)
DEMO_READY_EVENTS = ("match_demo_ready", "demo_ready")
# Parça boyutu: Cloudflare ücretsiz tüneli tek istekte ~100 MB'a izin verir
CHUNK_BYTES = 32 * 1024 * 1024
# Bir oyuncunun aynı anda açık (yüklenen / sırada / işlenen) en fazla demo sayısı
MAX_OPEN_UPLOADS = 5
# Tarayıcıda üretilen demo paketi (gzip'li JSON, ~0,3-0,5 MB) için üst sınır
PACKAGE_MAX_BYTES = 8 * 1024 * 1024
STATIC_DIR = Path(__file__).with_name("static")
# Windows kayıt defterinde eksik olabilen türler: WASM akışla derlenebilsin, modül scriptleri yüklensin
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
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
    # FACEIT Downloads API (yetki açılana kadar kapalı): "FACEIT'ten getir" + otomatik demo
    faceit_downloads: bool = False
    webhook_secret: str = ""                    # DEMO_READY webhook gizli değeri (boşsa uç nokta 404)
    webhook_header: str = "X-Squadmind-Webhook"  # gizli değerin geldiği başlık adı
    mailer: Mailer | None = None                # ayarlı değilse doğrulama / şifre sıfırlama kapalı
    base_url: str = ""                          # e-postadaki bağlantılar için (boşsa istek adresi)
    login_limiter: acc.LoginLimiter | None = None  # testler kendi sınırlayıcısını verebilir
    # E-posta + şifre / nickname-davet girişi ve kayıt: kullanıcı kararı (24.10) ile arayüzden
    # kalktı, yalnız FACEIT ile giriş var. Kod duruyor; .env WEB_PASSWORD_LOGIN=1 ile geri açılır
    password_login: bool = False
    # KVKK / GDPR: ad, soyad, doğum tarihi, ihlaller varsayılan SAKLANMAZ (.env FACEIT_STORE_SENSITIVE=1)
    store_sensitive: bool = False


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
    csrf_signer = URLSafeTimedSerializer(settings.secret, salt="squadmind-csrf")
    verify_signer = URLSafeTimedSerializer(settings.secret, salt="squadmind-eposta-dogrula")
    reset_signer = URLSafeTimedSerializer(settings.secret, salt="squadmind-sifre-sifirla")
    limiter = settings.login_limiter or acc.LoginLimiter()
    mailer = settings.mailer if settings.mailer and settings.mailer.configured else None
    pw = settings.password_login
    # Şifre girişi kapalıyken giriş adresi ana sayfadır (orada yalnız FACEIT düğmesi var)
    login_url = "/giris" if pw else "/"
    store.map_name = settings.map_name
    # WASM ayrıştırıcı ve zstd çözücü (kendi sunucumuzdan; CDN yok)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Site simgesi: logo işareti (static/img/logo-mark.svg) koyu yuvarlak kare üstünde; dosya yoksa eski nişangâh
    _FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                '<rect width="32" height="32" rx="7" fill="#0d0f12"/>'
                '<circle cx="16" cy="16" r="8.5" fill="none" stroke="#ff6a2b" stroke-width="2.5"/>'
                '<circle cx="16" cy="16" r="2.6" fill="#ff6a2b"/></svg>')
    try:
        _mark = (STATIC_DIR / "img" / "logo-mark.svg").read_text(encoding="utf-8")
        _FAVICON = _mark.replace('viewBox="455 415 1020 1130"', 'viewBox="395 415 1140 1140"').replace(
            "<defs>", '<rect x="395" y="415" width="1140" height="1140" rx="210" fill="#0d1018"/><defs>', 1)
    except OSError:
        pass

    @app.get("/favicon.ico", include_in_schema=False)
    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return Response(_FAVICON, media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})

    # --- oturum ---------------------------------------------------------------------

    def _session_fp(steam_id: str) -> str:
        """Oturumun bağlı olduğu şifre parmak izi: şifre değişince eski oturumlar geçersizleşir."""
        found = acc.account_of(store, steam_id)
        return acc.reset_fingerprint(found[0].password_hash) if found and found[0].password_hash else ""

    def session_token(steam_id: str) -> str:
        return signer.dumps(f"{steam_id}|{_session_fp(steam_id)}")

    def current_user(request: Request) -> WebUser | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        try:
            payload = signer.loads(token, max_age=SESSION_DAYS * 86400)
        except BadSignature:
            return None
        steam_id, sep, fp = str(payload).partition("|")
        # Yeni biçim "steam_id|parmak izi": şifre değiştiyse eşleşmez. Eski biçim (yalnız steam_id)
        # yalnızca şifresi olmayan hesaplar için geçerli (geçiş; şifreli hesap yeni biçim ister)
        if not hmac.compare_digest(fp, _session_fp(steam_id)):
            return None
        if not sep and _session_fp(steam_id):
            return None
        return store.user(steam_id)

    def require_user(request: Request) -> WebUser:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "Oturum yok; davet linkiyle katıl.")
        return user

    _strip_cache: dict[str, Any] = {"at": 0.0, "v": {"n": 0, "players": 0, "min": SPOT_MIN_MATCHES}}

    def dust2_data_stats() -> dict[str, int]:
        """İşlenmiş Dust2 maç sayısı ve analiz edilen farklı oyuncu sayısı (30 sn önbellekli)."""
        if time.time() - _strip_cache["at"] < 30:
            return _strip_cache["v"]

        def q(con):
            n = con.execute("SELECT count(*) FROM matches WHERE demo_status = 'processed' "
                            "AND map = 'de_dust2'").fetchone()[0]
            m = con.execute("SELECT count(DISTINCT steam_id) FROM demo_player_stats WHERE match_id IN "
                            "(SELECT match_id FROM matches WHERE demo_status = 'processed' "
                            "AND map = 'de_dust2')").fetchone()[0]
            return int(n), int(m)
        try:
            n, m = store.run(q, lock=False)
            _strip_cache["v"] = {"n": n, "players": m, "min": SPOT_MIN_MATCHES}
        except Exception:  # noqa: BLE001
            log.exception("veri şeridi sayıları alınamadı")
        _strip_cache["at"] = time.time()
        return _strip_cache["v"]

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("me", current_user(request))
        if ctx["me"] is not None:
            ctx.setdefault("strip", dust2_data_stats())
        ctx.setdefault("owner", settings.team_owner)
        ctx.setdefault("faceit_downloads", settings.faceit_downloads)
        ctx.setdefault("password_login", pw)
        return TEMPLATES.TemplateResponse(request, name, ctx)

    # --- katılım --------------------------------------------------------------------

    def join_page(request: Request, davet: str = "", error: str | None = None) -> HTMLResponse:
        if error is None:
            # Açılır pencereden taşınan giriş hatası (tek seferlik)
            try:
                error = login_signer.loads(request.cookies.get(ERROR_COOKIE, ""), max_age=120)
            except BadSignature:
                error = None
        resp = page(request, "join.html", davet=davet, error=error,
                    closed=not settings.invite_code, faceit_login=oauth is not None,
                    invited=bool(davet), board=join_art())
        resp.squadmind_error = error
        if request.cookies.get(ERROR_COOKIE):
            resp.delete_cookie(ERROR_COOKIE)
        return resp

    def need_pw() -> None:
        """Şifre / nickname girişi kapalıyken bu uçlar yok sayılır (POST'lar 404)."""
        if not pw:
            raise HTTPException(404, "Bu giriş yolu kapalı; FACEIT ile giriş yap.")

    def to_home() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

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

    def is_teammate(faceit_id: str, nickname: str | None) -> bool:
        """Kadro sayfası erişimi: takım sahibinin kendisi ya da sahiple yeterince ortak maç."""
        return ((settings.team_owner or "").lower() == (nickname or "").lower()
                or same_team_matches(faceit_id) >= TEAM_MIN_SHARED)

    def faceit_email(user: dict) -> str | None:
        """FACEIT kullanıcı bilgisindeki e-posta (varsa, geçerliyse ve FACEIT doğrulanmamış demediyse)."""
        if user.get("email_verified") is False:
            return None
        try:
            return acc.normalize_email(str(user.get("email") or ""))
        except acc.AccountError:
            return None

    def split_user(user: dict) -> tuple[dict, dict]:
        """fetch_user yanıtı → (userinfo, anlık görüntü girdileri). Alt çizgili alanlar ayrılır."""
        info = {k: v for k, v in user.items() if not k.startswith("_")}
        return info, {"userinfo": info, "id_claims": user.get("_id_claims"),
                      "token_meta": user.get("_token_meta")}

    def session_response(request: Request, steam_id: str, target: str = "/ben") -> RedirectResponse:
        """Oturum çerezini kurup yönlendirir (eski çerezle aynı biçim: steam_id)."""
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(COOKIE, session_token(steam_id), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
        resp.delete_cookie(LOGIN_COOKIE)
        return resp

    def popup_bridge(resp: Response) -> Response:
        """FACEIT dönüşü açılır pencerede biter: çerezi kur, ana pencereyi hedefe gönder, kapan.

        FACEIT Connect giriş sayfası bir pop-up'ta çalışacak şekilde tasarlanmış (belge v3.0:
        "A pop-up is opened and directs the user to FACEIT Connect"); tam sayfada açılınca hesap
        seçimi tepki vermiyor. Pop-up yoksa (engellendiyse, doğrudan açıldıysa) aynı pencere gider.
        """
        if not isinstance(resp, RedirectResponse):
            return resp
        target = json.dumps(resp.headers.get("location", "/ben"))
        html = ('<!doctype html><html lang="tr"><meta charset="utf-8"><title>SquadMind</title>'
                '<body style="background:#0d0f12;color:#e8eaed;font-family:system-ui,sans-serif">'
                '<p style="margin:40px;text-align:center">Giriş tamamlandı, yönlendiriliyorsun…</p>'
                '<script>(function(){var t=' + target + ';try{if(window.opener&&!window.opener.closed'
                '&&window.opener.location.origin===location.origin){window.opener.location.href=t;'
                'window.close();return;}}catch(e){}location.replace(t);})();</script></body></html>')
        out = HTMLResponse(html, headers={"Cache-Control": "no-store"})
        for key, value in resp.raw_headers:
            if key.lower() == b"set-cookie":
                out.raw_headers.append((key, value))
        return out

    def enter(request: Request, profile: dict, invited: bool, davet: str, verified: bool = False,
              email: str | None = None, snap: dict | None = None):
        """FACEIT profiliyle oturum aç.

        FACEIT girişinde (kimlik doğrulanmış) davet gerekmeden herkes girer, hesabı açılır ve
        kendi takımını kurabilir. Nickname ile katılım (yalnız WEB_PASSWORD_LOGIN=1) kimlik
        doğrulamadığı için davet şart. Takım sahibinin kadro sayfaları (takım raporu, taktik
        tahtası) için erişim: davet kodu, takım sahibinin kendisi ya da onunla aynı tarafta
        TEAM_MIN_SHARED maç; erişimi olmayan kendi verisini ve arenayı kullanır.
        """
        row = profile_to_player_row(profile)
        if not row.get("steam_id"):
            return join_page(request, davet, "Bu FACEIT hesabına bağlı CS2 / Steam hesabı bulunamadı.")
        teammate = verified and is_teammate(row["faceit_id"], row["nickname"])
        if not verified and not invited and store.user(row["steam_id"]) is None:
            return join_page(request, davet, "Davet kodu geçersiz. Linki takım arkadaşından tekrar iste.")
        store.run(lambda con: upsert_players(con, [row]))
        store.add_user(row["steam_id"], row["faceit_id"], row["nickname"],
                       team_access=invited or teammate)
        if verified:
            # FACEIT kimliği kanıtlandı: bağlı hesabı hazırla (yoksa otomatik aç, 'doğrulanmış')
            acct_id = acc.ensure_faceit_account(store, row["steam_id"], email)
            if acct_id:
                acc.touch_login(store, acct_id)
                if snap is not None:
                    # FACEIT'ten alınan ham veriler (anahtarlar değil): anlık görüntü + hesap alanları
                    fdata.record_login(store, acct_id, row["faceit_id"], snap["userinfo"],
                                       snap.get("id_claims"), snap.get("token_meta"), profile,
                                       store_sensitive=settings.store_sensitive)
        worker.submit(Job("sync", row["steam_id"]))  # her girişte maç geçmişi tazelenir
        return session_response(request, row["steam_id"])

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, davet: str = ""):
        if current_user(request):
            return RedirectResponse("/ben", status_code=303)
        return join_page(request, davet)

    @app.post("/katil", response_class=HTMLResponse)
    def join(request: Request, nickname: str = Form(...), davet: str = Form("")):
        need_pw()
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
    def faceit_login(request: Request, davet: str = "", popup: str = ""):
        if oauth is None:
            raise HTTPException(404, "FACEIT ile giriş ayarlı değil.")
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        if getattr(oauth, "pkce_method", "S256") == "plain":
            challenge = verifier          # plain: challenge = doğrulayıcının kendisi (RFC 7636 §4.2)
        resp = RedirectResponse(oauth.authorize_url(state, challenge), status_code=303)
        # Tanı (24.10): challenge ve state'in başı; FACEIT Connect sayfasının /api/v1/authorize
        # isteğinde ilettiği code_challenge ile karşılaştırmak için (ikisi de adreste açık değerler)
        log.warning("FACEIT giriş başladı: state=%s… challenge=%s… popup=%s",
                    state[:6], challenge[:10], popup == "1")
        # "p": giriş açılır pencerede başladı (dönüşte ana pencereye köprü sayfası döner)
        resp.set_cookie(LOGIN_COOKIE, login_signer.dumps({"s": state, "v": verifier, "d": davet,
                                                          "p": popup == "1"}),
                        max_age=LOGIN_SECONDS, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp

    @app.get("/giris/faceit/donus", response_class=HTMLResponse)
    def faceit_callback(request: Request, code: str = "", state: str = "", error: str = "",
                        error_description: str = ""):
        # Tanı (24.10): FACEIT dönüşe yayıncıyı (iss, RFC 9207) ekliyor; token adresi yayıncıya göre
        log.warning("FACEIT dönüşü: iss=%s kod=%s hata=%s", request.query_params.get("iss", "-")[:80],
                    "var" if code else "yok", error[:40] or "-")
        resp = _faceit_callback(request, code, state, error, error_description)
        popup = False
        try:
            popup = bool(login_signer.loads(request.cookies.get(LOGIN_COOKIE, ""),
                                            max_age=LOGIN_SECONDS).get("p"))
        except BadSignature:
            pass
        if popup and isinstance(resp, RedirectResponse):
            return popup_bridge(resp)
        if popup:
            # Hata sayfası açılır pencerede kalmasın: mesaj kısa ömürlü imzalı çerezle ana sayfaya
            msg = getattr(resp, "squadmind_error", None) or "FACEIT girişi tamamlanamadı."
            bridge = popup_bridge(RedirectResponse("/", status_code=303))
            bridge.set_cookie(ERROR_COOKIE, login_signer.dumps(msg), max_age=120, httponly=True,
                              samesite="lax", secure=request.url.scheme == "https")
            return bridge
        return resp

    def _faceit_callback(request: Request, code: str, state: str, error: str, error_description: str):
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
            # FACEIT hata kodunu dönüş adresine yazar (ör. access_denied); günlük ve kullanıcı için
            log.warning("FACEIT dönüşü kodsuz: %s %s", error[:80], error_description[:200])
            detail = f" ({error[:80]})" if error else ""
            return join_page(request, davet, f"FACEIT girişi tamamlanmadı{detail}.")
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
        done = enter(request, profile, invite_ok(davet), davet, verified=True, email=faceit_email(user),
                     snap=split_user(user)[1])
        return done

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
        done = enter(request, profile, invite_ok(davet), davet, verified=True, email=faceit_email(user),
                     snap=split_user(user)[1])
        return popup_bridge(done) if pending.get("p") else done

    # --- hesaplar: e-posta ile kayıt / giriş, hesap sayfası ---------------------------------

    FLASH = {"eklendi": "E-posta eklendi.", "sifre": "Şifren güncellendi. Şimdi giriş yapabilirsin.",
             "dogrulandi": "E-posta doğrulandı.", "gonderildi": "Doğrulama e-postası yollandı.",
             "degisti": "Şifren değiştirildi."}
    CSRF_MSG = "Form süresi doldu ya da doğrulanamadı; sayfayı yenileyip tekrar dene."
    BAD_LINK = "Bağlantı geçersiz, süresi dolmuş ya da daha önce kullanılmış. Yenisini iste."

    def client_ip(request: Request) -> str:
        # Cloudflare tüneli gerçek IP'yi başlıkta taşır; yoksa doğrudan bağlantı adresi
        return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")

    def base_link(request: Request) -> str:
        return (settings.base_url or str(request.base_url)).rstrip("/")

    def form_page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        """Form içeren sayfa: CSRF çerezi yoksa kurar, forma imzalı kopyasını koyar."""
        raw = request.cookies.get(CSRF_COOKIE, "")
        new = not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", raw)
        if new:
            raw = secrets.token_urlsafe(24)
        ctx["csrf"] = csrf_signer.dumps(raw)
        ctx.setdefault("mail_on", mailer is not None)
        resp = page(request, name, **ctx)
        resp.status_code = status
        if new:
            resp.set_cookie(CSRF_COOKIE, raw, max_age=CSRF_SECONDS, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https")
        return resp

    def csrf_ok(request: Request, token: str) -> bool:
        raw = request.cookies.get(CSRF_COOKIE, "")
        if not raw or not token:
            return False
        try:
            return hmac.compare_digest(csrf_signer.loads(token, max_age=CSRF_SECONDS), raw)
        except BadSignature:
            return False

    def need_mail() -> Mailer:
        if mailer is None:
            raise HTTPException(404, "E-posta gönderimi ayarlı değil.")
        return mailer

    def send_verification(request: Request, account: acc.Account) -> bool:
        """Doğrulama bağlantısı yollar (e-posta ayarlıysa); bağlantı günlüğe yazılmaz."""
        if mailer is None or not account.email:
            return False
        token = verify_signer.dumps({"a": account.account_id, "e": account.email})
        body = ("SquadMind e-posta adresini doğrulamak için bağlantıya tıkla (24 saat geçerli):\n\n"
                f"{base_link(request)}/hesap/dogrula?t={token}\n\n"
                "Bu isteği sen yapmadıysan bu e-postayı yok say.")
        try:
            mailer.send(account.email, "SquadMind: e-postanı doğrula", body)
        except MailError:
            return False
        return True

    def cookie_account(request: Request) -> tuple[WebUser, acc.Account | None, str | None] | None:
        """Oturumdaki kullanıcı, bağlı hesabı ve FACEIT bağlantı durumu."""
        user = current_user(request)
        if user is None:
            return None
        found = acc.account_of(store, user.steam_id)
        if found is None and not pw:
            found = ensure_plain_account(user.steam_id)  # eski oturum: hesap sessizce açılır
        return (user, found[0], found[1]) if found else (user, None, None)

    def ensure_plain_account(steam_id: str) -> tuple[acc.Account, str | None] | None:
        """Hesabı olmayan oyuncuya e-postasız hesap açar (bağlantı 'dogrulanmamis')."""
        try:
            account_id = acc.create(store, None, None, False)
            acc.link_user(store, account_id, steam_id, acc.UNVERIFIED)
        except acc.AccountError:
            return None
        return acc.account_of(store, steam_id)

    @app.get("/kayit", response_class=HTMLResponse)
    def register_form(request: Request, davet: str = ""):
        if not pw:
            return to_home()
        if current_user(request):
            return RedirectResponse("/ben", status_code=303)
        return form_page(request, "auth.html", kind="kayit", davet=davet,
                         closed=not settings.invite_code, faceit_login=oauth is not None)

    @app.post("/kayit", response_class=HTMLResponse)
    def register(request: Request, email: str = Form(""), password: str = Form(""),
                 nickname: str = Form(""), davet: str = Form(""), csrf: str = Form("")):
        need_pw()
        ctx = {"kind": "kayit", "davet": davet, "email": email, "nickname": nickname,
               "closed": not settings.invite_code, "faceit_login": oauth is not None}
        if not csrf_ok(request, csrf):
            return form_page(request, "auth.html", 400, error=CSRF_MSG, **ctx)
        # Nickname kimliği kanıtlamaz: katılımla aynı kural, davet kodu her zaman şart
        if not invite_ok(davet):
            return form_page(request, "auth.html", 400, error="Davet kodu geçersiz. Linki takım arkadaşından tekrar iste.", **ctx)
        try:
            mail = acc.normalize_email(email)
            acc.check_password_policy(password, mail)
            if acc.by_email(store, mail):
                raise acc.AccountError("Bu e-posta ile zaten bir hesap var. Giriş yapmayı dene.")
        except acc.AccountError as exc:
            return form_page(request, "auth.html", 400, error=str(exc), **ctx)
        try:
            with client_factory() as client:
                profile = client.get_player_by_nickname(nickname.strip())
        except FaceitNotFound:
            return form_page(request, "auth.html", 400, error=f"FACEIT'te '{nickname.strip()}' bulunamadı. Büyük / küçük harfe dikkat et.", **ctx)
        except FaceitAPIError as exc:
            return form_page(request, "auth.html", 400, error=f"FACEIT'e ulaşılamadı: {exc}", **ctx)
        row = profile_to_player_row(profile)
        if not row.get("steam_id"):
            return form_page(request, "auth.html", 400, error="Bu FACEIT hesabına bağlı CS2 / Steam hesabı bulunamadı.", **ctx)
        if store.user(row["steam_id"]) is not None:
            # Daha önce katılmış oyuncuyu e-postayla sahiplenmek, nickname bilen herkese açık olmasın
            msg = ("Bu FACEIT hesabı zaten bir hesaba bağlı." if acc.account_of(store, row["steam_id"])
                   else "Bu FACEIT hesabı sitede zaten kayıtlı. Eski oturumunla ya da FACEIT ile girip e-postanı orada ekle.")
            return form_page(request, "auth.html", 400, error=msg, **ctx)
        store.run(lambda con: upsert_players(con, [row]))
        store.add_user(row["steam_id"], row["faceit_id"], row["nickname"], team_access=True)
        try:
            account_id = acc.create(store, mail, acc.hash_password(password), False)
            acc.link_user(store, account_id, row["steam_id"], acc.UNVERIFIED)
        except acc.AccountError as exc:
            return form_page(request, "auth.html", 400, error=str(exc), **ctx)
        created = acc.by_id(store, account_id)
        if created:
            send_verification(request, created)
        acc.touch_login(store, account_id)
        worker.submit(Job("sync", row["steam_id"]))
        return session_response(request, row["steam_id"])

    @app.get("/giris", response_class=HTMLResponse)
    def login_form(request: Request, m: str = ""):
        if not pw:
            return to_home()
        if current_user(request):
            return RedirectResponse("/ben", status_code=303)
        return form_page(request, "auth.html", kind="giris", flash=FLASH.get(m),
                         faceit_login=oauth is not None)

    @app.post("/giris", response_class=HTMLResponse)
    def login(request: Request, email: str = Form(""), password: str = Form(""),
              csrf: str = Form("")):
        need_pw()
        ctx = {"kind": "giris", "email": email, "faceit_login": oauth is not None}
        if not csrf_ok(request, csrf):
            return form_page(request, "auth.html", 400, error=CSRF_MSG, **ctx)
        mail, ip = email.strip().lower()[:254], client_ip(request)
        if limiter.blocked(mail, ip):
            return form_page(request, "auth.html", 429, error="Çok fazla hatalı deneme. 15 dakika sonra tekrar dene.", **ctx)
        account = acc.by_email(store, mail)
        # Hesap yoksa da karma doğrulanır: süre farkından hesap varlığı anlaşılmasın
        ok = acc.verify_password(password[: acc.MAX_PASSWORD],
                                 account.password_hash if account and account.password_hash else acc.DUMMY_HASH)
        steam_id = acc.steam_of(store, account.account_id) if account and ok else None
        if not (account and account.password_hash and ok and steam_id):
            limiter.fail(mail, ip)
            return form_page(request, "auth.html", 400, error="E-posta ya da şifre hatalı.", **ctx)
        limiter.reset(mail, ip)
        acc.touch_login(store, account.account_id)
        return session_response(request, steam_id)

    @app.get("/hesap", response_class=HTMLResponse)
    def account_page(request: Request, m: str = ""):
        ctx = cookie_account(request)
        if ctx is None:
            return RedirectResponse(login_url, status_code=303)
        user, account, link = ctx
        if account is None:
            return RedirectResponse("/hesap/eposta" if pw else "/ben", status_code=303)
        info = store.run(lambda con: con.execute(
            "SELECT level, elo FROM players WHERE faceit_id = ?", [user.faceit_id]).fetchone())
        return form_page(request, "account.html", user=user, account=account, link=link,
                         has_password=bool(account.password_hash), flash=FLASH.get(m),
                         level=info[0] if info else None, elo=info[1] if info else None,
                         faceit_login=oauth is not None,
                         snapshots=[{**s, "fields": fdata.display_fields(s["payload"])}
                                    for s in fdata.latest_snapshots(store, account.account_id)])

    @app.get("/hesap/verilerim.json")
    def my_data(request: Request):
        """Yalnız oturumdaki kullanıcının kendi hesabı ve FACEIT verileri (JSON indirme)."""
        user = require_user(request)
        found = acc.account_of(store, user.steam_id)
        if found is None:
            raise HTTPException(404, "Bağlı hesap yok.")
        body = json.dumps(fdata.export_account(store, found[0].account_id), ensure_ascii=False,
                          indent=2, default=str)
        return Response(body, media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="squadmind-verilerim.json"',
                                 "Cache-Control": "no-store"})

    @app.get("/hesap/sil", response_class=HTMLResponse)
    def delete_form(request: Request):
        ctx = cookie_account(request)
        if ctx is None:
            return RedirectResponse(login_url, status_code=303)
        if ctx[1] is None:
            return RedirectResponse("/ben", status_code=303)
        return form_page(request, "account_delete.html", user=ctx[0], account=ctx[1])

    @app.post("/hesap/sil")
    def delete_account(request: Request, csrf: str = Form(""), onay: str = Form("")):
        ctx = cookie_account(request)
        if ctx is None:
            return RedirectResponse(login_url, status_code=303)
        if ctx[1] is None:
            return RedirectResponse("/ben", status_code=303)
        if not csrf_ok(request, csrf):
            return form_page(request, "account_delete.html", 400, user=ctx[0], account=ctx[1],
                             error=CSRF_MSG)
        if onay.strip().upper() != "SIL":
            return form_page(request, "account_delete.html", 400, user=ctx[0], account=ctx[1],
                             error="Onay için kutuya SIL yaz.")
        fdata.delete_account(store, ctx[1].account_id)
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/hesap/eposta", response_class=HTMLResponse)
    def add_email_form(request: Request):
        if not pw:
            return to_home()
        ctx = cookie_account(request)
        if ctx is None:
            return RedirectResponse("/", status_code=303)
        if ctx[1] is not None and ctx[1].email:
            return RedirectResponse("/hesap", status_code=303)
        return form_page(request, "auth.html", kind="eposta")

    @app.post("/hesap/eposta", response_class=HTMLResponse)
    def add_email(request: Request, email: str = Form(""), password: str = Form(""),
                  csrf: str = Form("")):
        """Eski oturum (hesapsız) ya da e-postasız FACEIT hesabı: e-posta + şifre ekler."""
        need_pw()
        ctx = cookie_account(request)
        if ctx is None:
            return RedirectResponse("/", status_code=303)
        user, account, _ = ctx
        view = {"kind": "eposta", "email": email}
        if not csrf_ok(request, csrf):
            return form_page(request, "auth.html", 400, error=CSRF_MSG, **view)
        if account is not None and account.email:
            return RedirectResponse("/hesap", status_code=303)
        try:
            mail = acc.normalize_email(email)
            acc.check_password_policy(password, mail)
            pw_hash = acc.hash_password(password)
            if account is None:
                account_id = acc.create(store, mail, pw_hash, False)
                acc.link_user(store, account_id, user.steam_id, acc.UNVERIFIED)
            else:
                account_id = account.account_id
                acc.set_email(store, account_id, mail, False)
                acc.set_password(store, account_id, pw_hash)
        except acc.AccountError as exc:
            return form_page(request, "auth.html", 400, error=str(exc), **view)
        created = acc.by_id(store, account_id)
        if created:
            send_verification(request, created)
        # Şifre yeni kondu: oturum çerezi yeni parmak iziyle yenilenir (yoksa kullanıcı düşer)
        resp = RedirectResponse("/hesap?m=eklendi", status_code=303)
        resp.set_cookie(COOKIE, session_token(user.steam_id), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return resp

    @app.get("/hesap/atla")
    def skip_email():
        """E-posta ekleme ekranını bir gün ertele (eski oturumlar zorlanmasın)."""
        resp = RedirectResponse("/ben", status_code=303)
        resp.set_cookie(SKIP_COOKIE, "1", max_age=86400, httponly=True, samesite="lax")
        return resp

    @app.post("/hesap/sifre", response_class=HTMLResponse)
    def change_password(request: Request, current: str = Form(""), password: str = Form(""),
                        csrf: str = Form("")):
        need_pw()
        ctx = cookie_account(request)
        if ctx is None or ctx[1] is None:
            return RedirectResponse(login_url, status_code=303)
        user, account, link = ctx
        view = {"user": user, "account": account, "link": link, "faceit_login": oauth is not None,
                "has_password": bool(account.password_hash)}
        if not csrf_ok(request, csrf):
            return form_page(request, "account.html", 400, error=CSRF_MSG, **view)
        key, ip = f"sifre:{account.account_id}", client_ip(request)
        if limiter.blocked(key, ip):
            return form_page(request, "account.html", 429, error="Çok fazla hatalı deneme. 15 dakika sonra tekrar dene.", **view)
        if account.password_hash and not acc.verify_password(current[: acc.MAX_PASSWORD], account.password_hash):
            limiter.fail(key, ip)
            return form_page(request, "account.html", 400, error="Mevcut şifre hatalı.", **view)
        try:
            acc.check_password_policy(password, account.email)
        except acc.AccountError as exc:
            return form_page(request, "account.html", 400, error=str(exc), **view)
        acc.set_password(store, account.account_id, acc.hash_password(password))
        # Diğer cihazlardaki oturumlar düşer (parmak izi değişti); bu oturuma yeni çerez
        resp = RedirectResponse("/hesap?m=degisti", status_code=303)
        resp.set_cookie(COOKIE, session_token(user.steam_id), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return resp

    @app.post("/hesap/dogrulama-gonder")
    def resend_verification(request: Request, csrf: str = Form("")):
        need_pw()
        need_mail()
        ctx = cookie_account(request)
        if ctx is None or ctx[1] is None or not ctx[1].email:
            return RedirectResponse("/giris", status_code=303)
        if not csrf_ok(request, csrf):
            raise HTTPException(400, CSRF_MSG)
        key, ip = f"dogrulama:{ctx[1].account_id}", client_ip(request)
        if limiter.blocked(key, ip):
            raise HTTPException(429, "Çok sık istendi; biraz bekle.")
        limiter.fail(key, ip)  # her istek sayılır: e-posta bombası olmasın
        send_verification(request, ctx[1])
        return RedirectResponse("/hesap?m=gonderildi", status_code=303)

    @app.get("/hesap/dogrula")
    def verify_email(request: Request, t: str = ""):
        if not pw:
            return to_home()
        need_mail()
        bad = "Doğrulama bağlantısı geçersiz ya da süresi dolmuş."
        try:
            data = verify_signer.loads(t, max_age=VERIFY_SECONDS)
        except BadSignature:
            return form_page(request, "auth.html", 400, kind="giris", error=bad, faceit_login=oauth is not None)
        account = acc.by_id(store, str(data.get("a", "")))
        if account is None or account.email != data.get("e"):
            return form_page(request, "auth.html", 400, kind="giris", error=bad, faceit_login=oauth is not None)
        acc.mark_verified(store, account.account_id)
        return RedirectResponse("/hesap?m=dogrulandi" if current_user(request) else "/giris?m=dogrulandi",
                                status_code=303)

    @app.get("/sifre-unuttum", response_class=HTMLResponse)
    def forgot_form(request: Request):
        if not pw:
            return to_home()
        need_mail()
        return form_page(request, "auth.html", kind="unuttum")

    @app.post("/sifre-unuttum", response_class=HTMLResponse)
    def forgot(request: Request, email: str = Form(""), csrf: str = Form("")):
        need_pw()
        need_mail()
        if not csrf_ok(request, csrf):
            return form_page(request, "auth.html", 400, kind="unuttum", error=CSRF_MSG)
        mail, ip = email.strip().lower()[:254], client_ip(request)
        key = f"unuttum:{mail}"
        # Her durumda aynı yanıt: hesap var mı yok mu anlaşılmasın; istek sayısı sınırlı
        if not limiter.blocked(key, ip):
            limiter.fail(key, ip)
            account = acc.by_email(store, mail)
            if account and acc.steam_of(store, account.account_id):
                token = reset_signer.dumps({"a": account.account_id, "e": account.email,
                                            "h": acc.reset_fingerprint(account.password_hash)})
                body = ("SquadMind şifreni yenilemek için bağlantıya tıkla (1 saat geçerli, bir kez kullanılır):\n\n"
                        f"{base_link(request)}/sifre-sifirla?t={token}\n\n"
                        "Bu isteği sen yapmadıysan bu e-postayı yok say; şifren değişmez.")
                try:
                    need_mail().send(account.email, "SquadMind: şifre sıfırlama", body)
                except MailError:
                    pass  # kullanıcıya hata sızdırma (hesap varlığı anlaşılmasın)
        return form_page(request, "auth.html", kind="unuttum", sent=True)

    def reset_account(t: str) -> acc.Account | None:
        """Geçerli, süresi dolmamış ve kullanılmamış sıfırlama bağlantısının hesabı."""
        try:
            data = reset_signer.loads(t, max_age=RESET_SECONDS)
        except BadSignature:
            return None
        account = acc.by_id(store, str(data.get("a", "")))
        if (account is None or account.email != data.get("e")
                or not hmac.compare_digest(acc.reset_fingerprint(account.password_hash),
                                           str(data.get("h", "")))):
            return None
        return account

    @app.get("/sifre-sifirla", response_class=HTMLResponse)
    def reset_form(request: Request, t: str = ""):
        if not pw:
            return to_home()
        need_mail()
        if reset_account(t) is None:
            return form_page(request, "auth.html", 400, kind="unuttum", error=BAD_LINK)
        return form_page(request, "auth.html", kind="sifirla", token=t)

    @app.post("/sifre-sifirla", response_class=HTMLResponse)
    def reset(request: Request, t: str = Form(""), password: str = Form(""), csrf: str = Form("")):
        need_pw()
        need_mail()
        if not csrf_ok(request, csrf):
            return form_page(request, "auth.html", 400, kind="sifirla", token=t, error=CSRF_MSG)
        account = reset_account(t)
        if account is None:
            return form_page(request, "auth.html", 400, kind="unuttum", error=BAD_LINK)
        try:
            acc.check_password_policy(password, account.email)
        except acc.AccountError as exc:
            return form_page(request, "auth.html", 400, kind="sifirla", token=t, error=str(exc))
        acc.set_password(store, account.account_id, acc.hash_password(password))
        acc.mark_verified(store, account.account_id)  # bağlantı e-postaya gitti: sahiplik kanıtı
        return RedirectResponse("/giris?m=sifre", status_code=303)

    # --- gizlilik / kullanım şartları (FACEIT onay ekranı bu adreslere bağlanır) ---------

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return page(request, "legal.html", page="privacy", title="Gizlilik")

    @app.get("/terms", response_class=HTMLResponse)
    def terms(request: Request):
        return page(request, "legal.html", page="terms", title="Kullanım şartları")

    @app.get("/giris/link")
    def login_link(request: Request, t: str = ""):
        """`squadmind login-link <nick>` ile üretilen imzalı link: oturum açar (FACEIT yedeği).

        Link WEB_SECRET ile imzalı (yönetici üretir) ve LINK_SECONDS geçerli. Yük: yeni biçimde
        {s: steam_id, f: faceit_id, n: nickname}, eski biçimde düz steam_id. Kayıtlı olmayan
        oyuncu için kullanıcı + e-postasız hesap açılır ('dogrulanmamis': kimliği FACEIT değil
        yönetici doğruladı); kadro erişimi FACEIT girişiyle aynı kuralla.
        """
        try:
            payload = URLSafeTimedSerializer(settings.secret, salt=LINK_SALT).loads(
                t, max_age=LINK_SECONDS)
        except BadSignature:
            return join_page(request, "", "Giriş linkinin süresi dolmuş ya da geçersiz.")
        if isinstance(payload, dict):
            steam_id = str(payload.get("s", ""))
            faceit_id, nickname = str(payload.get("f", "")), str(payload.get("n", ""))
        else:  # eski biçim: yalnız kayıtlı kullanıcı
            steam_id, faceit_id, nickname = str(payload), "", ""
        if not steam_id:
            return join_page(request, "", "Giriş linkinin süresi dolmuş ya da geçersiz.")
        if store.user(steam_id) is None:
            if not (faceit_id and nickname):
                return join_page(request, "", "Bu hesap siteye kayıtlı değil.")
            store.add_user(steam_id, faceit_id, nickname,
                           team_access=is_teammate(faceit_id, nickname))
            worker.submit(Job("sync", steam_id))
        if acc.account_of(store, steam_id) is None:
            ensure_plain_account(steam_id)
        found = acc.account_of(store, steam_id)
        if found:
            acc.touch_login(store, found[0].account_id)
        return session_response(request, steam_id)

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
        found = acc.account_of(store, user.steam_id)
        if found is None and not pw:
            found = ensure_plain_account(user.steam_id)  # eski oturum: hesap sessizce açılır
        if found is None and pw and not request.cookies.get(SKIP_COOKIE):
            return RedirectResponse("/hesap/eposta", status_code=303)  # eski oturum: hesap burada açılır
        uploads = store.uploads_of(user.steam_id)
        cards = map_cards(user)
        return page(request, "overview.html", acct=found[0] if found else None,
                    acct_link=found[1] if found else None, user=user, cards=cards, totals=store.totals(user.steam_id),
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
                    chunk=CHUNK_BYTES, package_max=PACKAGE_MAX_BYTES, sizes=(demo_rules.MIN_DEMO_BYTES, demo_rules.MAX_DEMO_BYTES))

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

    # --- tarayıcıda işlenen paket (varsayılan yol; tam demo yüklemesi yedek) -------------------

    @app.post("/api/paket")
    async def package_upload(request: Request, name: str = "", sha256: str = "",
                             dem_sha256: str = "", size: int = 0, map: str = "",
                             kaynak: str = ""):
        """Tarayıcıda WASM ile üretilen gzip'li paketi alır; işçi kuyruğuna koyar.

        Gövde = ham gzip baytları. Sorgu: name (orijinal dosya adı), sha256 (seçilen dosyanın
        olduğu gibi baytlarının özeti), dem_sha256 (açılmış .dem özeti), size (orijinal boyut), map.
        """
        user = require_user(request)
        name = safe_file_name(name)
        if not name.lower().endswith(demo_rules.DEMO_SUFFIXES):
            raise HTTPException(400, "Sadece .dem, .dem.zst ya da .dem.gz işlenebilir.")
        if map not in tb.MAP_POOL:
            raise HTTPException(400, "Önce haritayı seç.")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > PACKAGE_MAX_BYTES:
            raise HTTPException(413, "Paket çok büyük; tam demoyu yükle.")
        data = await request.body()
        if len(data) > PACKAGE_MAX_BYTES:
            raise HTTPException(413, "Paket çok büyük; tam demoyu yükle.")
        if data[:2] != b"\x1f\x8b":
            raise HTTPException(400, "Paket gzip değil.")
        if store.open_uploads(user.steam_id) >= MAX_OPEN_UPLOADS:
            raise HTTPException(429, "Önceki yüklemelerin işlenmesini bekle.")
        # Hızlı geri bildirim: kopya ise işçiyi hiç beklemeden reddet (işçi yine denetler)
        reason = store.run(lambda con: demo_rules.precheck_package(con, name, sha256, dem_sha256))
        if reason:
            raise HTTPException(409, reason)
        upload_id = store.new_upload(user.steam_id, name, size or len(data), map)
        folder = settings.uploads_dir / upload_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "paket.json.gz"
        path.write_bytes(data)
        store.set_upload(upload_id, QUEUED)
        worker.submit(Job("package", upload_id, path, {
            "sha256": sha256, "dem_sha256": dem_sha256, "orig_size": size, "file_name": name,
            "source": "faceit-tarayici" if kaynak == "faceit" and settings.faceit_downloads
                      else "paket"}))
        return {"id": upload_id}

    # --- FACEIT'ten getir (Downloads API; FACEIT_DOWNLOADS_ENABLED=1 olmadan kapalı) ------------

    rate_log: dict[str, collections.deque] = {}

    def downloads_user(request: Request) -> WebUser:
        """Bayrak kapalıysa 404 (uç nokta yokmuş gibi); sonra oturum şart."""
        if not settings.faceit_downloads:
            raise HTTPException(404, "Bulunamadı.")
        return require_user(request)

    def rate_limit(user: WebUser) -> None:
        """Kullanıcı başı kayan 60 sn pencere: imzalı link / sunucu işi istekleri."""
        now = time.monotonic()
        q = rate_log.setdefault(user.steam_id, collections.deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= FACEIT_RATE_PER_MIN:
            raise HTTPException(429, "Çok sık istek; biraz bekle.")
        q.append(now)

    def checked_match(user: WebUser, match_id: str) -> dict:
        """Kullanıcı o maçta oynamış ve maç bizde işlenmemiş olmalı; harita havuzda olmalı."""
        if not re.fullmatch(r"1-[0-9a-f-]{36}", match_id):
            raise HTTPException(400, "Maç kimliği geçersiz.")
        row = store.played(user.faceit_id, match_id)
        if row is None:
            raise HTTPException(403, "Bu maçta oynamamışsın.")
        if row["processed"]:
            raise HTTPException(409, "Bu maçın demosu zaten işlendi.")
        if row["map"] not in tb.MAP_POOL:
            raise HTTPException(400, "Bu harita henüz desteklenmiyor.")
        return row

    async def match_id_of(request: Request) -> str:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Gövde JSON olmalı.") from None
        return str(body.get("match_id", "")) if isinstance(body, dict) else ""

    @app.get("/api/faceit/maclar")
    def faceit_matches(request: Request):
        user = downloads_user(request)
        return JSONResponse({"matches": [
            {**m, "date": m["date"].isoformat() if m["date"] else None,
             "label": tb.map_label(m["map"]) if m["map"] else "",
             "supported": m["map"] in tb.MAP_POOL}
            for m in store.faceit_matches(user.faceit_id)]})

    @app.post("/api/faceit/demo-link")
    async def faceit_demo_link(request: Request):
        """İmzalı indirme linki (cache yok): tarayıcı demoyu kendisi indirip paketler."""
        user = downloads_user(request)
        match_id = await match_id_of(request)
        row = checked_match(user, match_id)
        rate_limit(user)
        try:
            with client_factory() as client:
                resource = demo_resource(store, client, match_id)
                if not resource:
                    raise HTTPException(404, "Bu maçın demosu henüz yok.")
                url = client.get_demo_download_url(resource)
        except FaceitNotFound:
            raise HTTPException(404, "Demo bulunamadı (süresi geçmiş olabilir).") from None
        except FaceitAPIError as exc:
            log.warning("demo-link: %s", exc)
            raise HTTPException(502, "FACEIT demo linki vermedi; sunucu yolunu dene.") from None
        ext = ".dem.gz" if urlsplit(url).path.endswith(".gz") else ".dem.zst"
        return JSONResponse({"url": url, "name": f"{match_id}{ext}", "map": row["map"]},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/faceit/sunucu-isle")
    async def faceit_server_ingest(request: Request):
        """Yedek: CORS yüzünden tarayıcı indiremezse sunucu indirir, işler, dosyayı siler."""
        user = downloads_user(request)
        match_id = await match_id_of(request)
        row = checked_match(user, match_id)
        rate_limit(user)
        if store.open_uploads(user.steam_id) >= MAX_OPEN_UPLOADS:
            raise HTTPException(429, "Önceki yüklemelerin işlenmesini bekle.")
        upload_id = store.new_upload(user.steam_id, f"{match_id}.dem.zst", 0, row["map"])
        store.set_upload(upload_id, QUEUED)
        worker.submit(Job("faceit", upload_id, meta={"match_id": match_id}))
        return {"id": upload_id}

    @app.post("/ben/otomatik-demo")
    def auto_demo_toggle(request: Request, acik: str = Form("")):
        """Otomatik demo izni: maçının demosu hazır olunca sunucu getirip işler."""
        user = downloads_user(request)
        store.set_auto_demo(user.steam_id, acik == "1")
        return RedirectResponse("/ben", status_code=303)

    @app.post("/api/faceit/webhook")
    async def faceit_webhook(request: Request, gizli: str = ""):
        """FACEIT DEMO_READY (match_demo_ready) olayı.

        Doğrulama: App Studio'da tanımlanan başlık adı/değeri (ya da ?gizli=) FACEIT_WEBHOOK_SECRET
        ile sabit zamanlı karşılaştırılır. Aynı maç bir kez kaydedilir. İşleme yalnızca
        FACEIT_DOWNLOADS_ENABLED=1 iken ve maçta "otomatik demo" izni açık kullanıcı varsa olur.
        """
        if not settings.webhook_secret:
            raise HTTPException(404, "Bulunamadı.")
        given = request.headers.get(settings.webhook_header) or gizli
        if not given or not hmac.compare_digest(given.encode(), settings.webhook_secret.encode()):
            raise HTTPException(401, "Gizli değer geçersiz.")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Gövde JSON olmalı.") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Gövde JSON nesnesi olmalı.")
        event = str(body.get("event", "")).lower()
        if event not in DEMO_READY_EVENTS:
            return {"ok": True, "ignored": event}   # başka olay: 200 (FACEIT yeniden denemesin)
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        match_id = str(payload.get("id") or payload.get("match_id") or "")
        if not re.fullmatch(r"1-[0-9a-f-]{36}", match_id):
            raise HTTPException(400, "Maç kimliği yok ya da geçersiz.")
        urls = payload.get("demo_url") or payload.get("demo_urls") or payload.get("resource_url")
        resource = (urls[0] if isinstance(urls, list) and urls else urls) or None
        resource = resource if isinstance(resource, str) else None
        if not store.record_demo_event(match_id, resource, str(body.get("transaction_id") or "") or None):
            return {"ok": True, "duplicate": True}  # tekrar gönderim: yeniden işleme yok
        if not settings.faceit_downloads:
            return {"ok": True, "recorded": True}   # yetki kapalı: kaydet, işleme
        # Maç zaten işlenmişse ya da iznini açmış kullanıcı yoksa yalnız kayıt
        done = store.run(lambda con: demo_rules.match_already_processed(con, match_id))
        users = [] if done else store.auto_demo_users(match_id)
        if not users:
            store.set_event_status(match_id, "atlandı",
                                   "demo zaten işlendi" if done else "otomatik demo izni olan kullanıcı yok")
            return {"ok": True, "recorded": True}
        owner = users[0]   # aynı demo bir kez: tek kullanıcı adına
        played = store.played(owner.faceit_id, match_id)
        upload_id = store.new_upload(owner.steam_id, f"{match_id}.dem.zst", 0,
                                     (played or {}).get("map"))
        store.set_upload(upload_id, QUEUED)
        store.set_event_status(match_id, "kuyrukta", processed_for=owner.steam_id)
        worker.submit(Job("faceit", upload_id, meta={"match_id": match_id}))
        return {"ok": True, "queued": True}

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
        from squadmind.analysis import cards as cards_mod
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
                    # Oyuncu kartı (analysis/cards.py): ham düello kayıtları ve betimleyici girdisi
                    "data": d, "weapon_counts": cards_mod.weapon_counts(duel_df), "desc": {},
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

    def fit_weapons(duel_df, roster: list[dict], ctx, simple: bool = True):
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
        return rt.fit_weapon_effect(cls_t, cls_ct, dist, won, off, simple=simple)

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
                                         weapon=fit_weapons(bm["duels"], roster, ctx),
                                         # Sınıf × mesafe modeli: YALNIZ betimleyici satır (profesör
                                         # 23.10: ek kazancı sınamayı geçmedi, olasılığa girmez)
                                         weapon_d=fit_weapons(bm["duels"], roster, ctx, simple=False))
                ctx, feats, at = _rating_cache["ctx"], _rating_cache["feats"], _rating_cache["at"]
                now = time.time()
                for p in players:
                    if p["steam_id"] not in feats or now - at.get(p["steam_id"], 0) > 60:
                        try:
                            prof = client.get_player_live(p["faceit_id"])   # ELO her zaman FACEIT'ten
                            game = (prof.get("games") or {}).get("cs2") or {}
                            hist = rt.summarize_history(client.get_player_match_stats(p["faceit_id"]))
                            elo = rt._num(game.get("faceit_elo"))
                            _rating_cache.setdefault("level", {})[p["steam_id"]] = game.get("skill_level")
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

    # --- oyuncu kartı (analysis/cards.py; sayılara dokunulmaz, yalnız sunulur) ------------------

    _card_cache: dict[str, tuple[tuple, float, dict]] = {}   # steam_id → (model anahtarı, an, kart)

    def card_data(map_name: str, player: dict) -> dict:
        """Bir oyuncunun kartı (JSON sözleşmesi: cards.PlayerCard.to_dict). Genel puan canlı ELO'dan
        (κ=0; arenadaki ana olasılıkla aynı girdi); canlı ELO 60 sn kuralıyla `builder_ratings`
        üzerinden gelir. FACEIT'e ulaşılamazsa genel puan "veri yok" kalır ve kart önbelleğe girmez."""
        from squadmind.analysis import cards as cd

        bm = builder_model(map_name)
        key = (map_name, len(bm["match_ids"]))
        sid = player["steam_id"]
        hit = _card_cache.get(sid)
        if hit and hit[0] == key and time.time() - hit[1] < 60:
            return hit[2]
        builder_ratings(map_name, [player])
        feats = _rating_cache["feats0"].get(sid)
        if sid not in bm["desc"]:
            try:
                bm["desc"][sid] = cd.descriptives(bm["data"], sid)
            except Exception:  # noqa: BLE001 - betimleyici başarısızsa kart onsuz açılır
                log.exception("kart betimleyicileri hesaplanamadı: %s", player.get("nickname"))
                bm["desc"][sid] = ({}, 0, 0)
        level = _rating_cache.get("level", {}).get(sid) or player.get("level")
        # Tek güç (cards.fit_power): ağırlıklar demo sayısı değişince bir kez öğrenilir; başarısızsa
        # kart yalnız ELO'lu güçle açılır. ARENA OLASILIĞINA GİRMEZ (sınamayı geçmedi).
        ctx = _rating_cache.get("ctx")
        if "power" not in bm and ctx is not None:
            try:
                eo, so = cd.power_inputs(ctx.hist, ctx.pop)
                bm["power"] = cd.fit_power(bm["duels"], cd.component_counts(bm["data"]), eo, so)
            except Exception:  # noqa: BLE001 - tek güç kurulamazsa kart güçsüz bileşenle açılır
                log.exception("tek güç modeli kurulamadı")
                bm["power"] = None
        f1 = _rating_cache["feats"].get(sid)
        card = cd.player_card(sid, feats=feats, counts=bm["weapon_counts"], desc=bm["desc"][sid],
                              nickname=player.get("nickname"), level=level, power_model=bm.get("power"),
                              stat_adj=f1.stat_adj if f1 is not None else 0.0).to_dict()
        if feats is not None:
            _card_cache[sid] = (key, time.time(), card)
        return card

    @app.get("/api/kart/{steam_id}")
    def card_api(steam_id: str, request: Request):
        """Oyuncu kartı. İzin arenadaki `builder_players` kuralıyla aynı (kendisi, kabul etmiş
        takım üyeleri, kadro erişimi varsa çekirdek)."""
        user = require_user(request)
        allowed = {p["steam_id"]: p for p in builder_players(user, "de_dust2")}
        if steam_id not in allowed:
            raise HTTPException(403, "Bu oyuncunun kartını görme iznin yok.")
        try:
            return card_data("de_dust2", allowed[steam_id])
        except Exception:  # noqa: BLE001
            log.exception("kart kurulamadı")
            raise HTTPException(503, "Kart şu an hazırlanamadı.") from None

    @app.get("/kartlar", response_class=HTMLResponse)
    def cards_page(request: Request):
        """İzinli oyuncuların kartları: genel puana göre sıralı ızgara + iki kartı yan yana kıyas (tarayıcıda)."""
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        # Kartlar tarayıcıdan /api/kart/{id} ile paralel çekilir (FACEIT çağrıları sayfayı bekletmesin)
        players = [{"steam_id": p["steam_id"], "nickname": p["nickname"], "me": p["me"]}
                   for p in builder_players(user, "de_dust2")]
        return page(request, "kartlar.html", user=user, payload={"players": players, "me": user.steam_id})

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
                   "me": user.steam_id, "spots": dust2_data_stats()}
        return page(request, "arena.html", user=user, board=bm["board"], payload=payload,
                    w=bm["proj"]["w"], h=bm["proj"]["h"])

    @app.get("/api/arena/spot-durumu")
    def arena_spot_status(request: Request):
        """Spot önerisi ilerlemesi: işlenmiş Dust2 maçı / eşik (40); eşik altında sıralama kapalı."""
        require_user(request)
        v = dust2_data_stats()
        return {"n": v["n"], "min": v["min"], "players": v["players"], "open": v["n"] >= v["min"]}

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
            try:
                elo = max(100.0, min(3000.0, float(b.get("elo") or 1000)))   # ELO karakteri: 100-3000
            except (TypeError, ValueError):
                raise HTTPException(400, "ELO sayı olmalı.") from None
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
        # Can (profesör kararı 23.10: ana olasılığa eklenir; kart ayrıca "can eşitken" satırını gösterir)
        def _hp(v) -> float:
            try:
                return min(100.0, max(1.0, float(v)))
            except (TypeError, ValueError):
                return 100.0
        hp_a, hp_b = _hp(a.get("hp", 100)), _hp(b.get("hp", 100))
        m0 = rt.matchup(ctx.model, a["steam_id"], side_a, b_key, line, fa0, fb0, hp_a=hp_a, hp_b=hp_b, **wkw)
        m = rt.matchup(ctx.model, a["steam_id"], side_a, b_key, line, fa, fb, hp_a=hp_a, hp_b=hp_b, **wkw)
        if (hp_a, hp_b) != (100.0, 100.0):
            meq = rt.matchup(ctx.model, a["steam_id"], side_a, b_key, line, fa0, fb0, **wkw)
            out["hp"] = {"a": hp_a, "b": hp_b, "p_eq": round(meq.p, 3), "lo_eq": round(meq.lo, 3),
                         "hi_eq": round(meq.hi, 3)}
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
        out["desc"] = arena_descriptive(bm["duels"], a, b, wa, wb, dist, za, zb)
        return out

    # Arena betimleyici satırları: olasılığı DEĞİŞTİRMEZ (profesör kararı 23.10)
    _CLS_TR = {"rifle": "tüfek", "pistol": "tabanca", "awp": "AWP", "smg": "SMG", "scout": "Scout",
               "heavy": "ağır silah"}

    def arena_descriptive(duel_df, a: dict, b: dict, wa: str, wb: str, dist: float,
                          za: float | None, zb: float | None) -> dict:
        """Silah × mesafe kaydı (sınanmadı), oyuncuların kendi sınıf × mesafe kayıtları, yükseklik."""
        from squadmind.analysis import duels as du
        from squadmind.analysis import rating as rt
        band = rt.WEAPON_BANDS[rt.weapon_band(dist)]
        res: dict[str, Any] = {"band": band}
        wd = _rating_cache.get("weapon_d")
        if wd is not None and (wa, wb) != ("rifle", "rifle") and wa in _CLS_TR and wb in _CLS_TR:
            # A − B sınıf farkı bu dilimde (kovaryans dahil)
            wx = rt.weapon_x(wa, wb, dist, simple=False)
            logit, se = float(wx @ wd.beta), max(float(wx @ wd.cov @ wx), 0.0) ** 0.5
            sig = lambda v: 1 / (1 + 2.718281828459045 ** (-v))   # noqa: E731
            # O hücredeki düello sayısı: iki yönde bu sınıf çifti, bu dilimde
            pair = duel_df.filter(pl.col("band") == band) if duel_df.height else duel_df
            n = pair.filter(((pl.col("winner_cls") == wa) & (pl.col("loser_cls") == wb))
                            | ((pl.col("winner_cls") == wb) & (pl.col("loser_cls") == wa))).height if pair.height else 0
            lo, hi = sig(logit - 1.645 * se), sig(logit + 1.645 * se)
            if "heavy" in (wa, wb):
                label = "veri yok"
            elif "scout" in (wa, wb) or n < 100:
                label = "az veri"
            else:
                label = "belirgin" if lo > 0.5 or hi < 0.5 else "belirsiz"
            res["cell"] = {"a": _CLS_TR[wa], "b": _CLS_TR[wb], "p": round(sig(logit), 3),
                           "lo": round(lo, 3), "hi": round(hi, 3), "n": n, "label": label}
        rec = du.class_band_record(duel_df)
        def _own(p: dict, cls: str) -> dict | None:
            sid = p.get("steam_id")
            if not sid:
                return None
            r = rec.filter((pl.col("player") == sid) & (pl.col("cls") == cls) & (pl.col("band") == band))
            if r.height == 0:
                return {"cls": _CLS_TR.get(cls, cls), "wins": 0, "losses": 0}
            row = r.row(0, named=True)
            return {"cls": _CLS_TR.get(cls, cls), "wins": int(row["wins"]), "losses": int(row["losses"])}
        res["own_a"], res["own_b"] = _own(a, wa), _own(b, wb)
        # Yükseklik: |dz| > 192'de yüksek olan %70 (n=43) — yalnız not, olasılığa girmez
        if za is not None and zb is not None and abs(za - zb) > 192:
            res["height"] = {"up": "a" if za > zb else "b", "dz": round(abs(za - zb))}
        return res

    # --- arena taktik modu: TEK örnek akış (analysis/replay.py) --------------------------
    # Profesör kararı 24.10: simülasyon sınamayı geçmedi → yanıtta olasılık / skor / kazanan
    # alanı YOK; yalnız zamanlı konumlar ve olay sırası. Etiket sabit (replay.FLOW_LABEL).

    _taktik_cache: dict[str, Any] = {}

    def taktik_board() -> dict:
        """Tahta SVG'si ve izdüşüm (DB'siz; harita geometrisinden), süreç boyunca önbellekli."""
        if "board" not in _taktik_cache:
            from squadmind.analysis.maps import BOARD_PLACES
            from squadmind.viz.board import ARENA, Board, _Projection, map_polygons, render_board
            width, pad = 640, 16
            proj = _Projection(map_polygons("de_dust2"), width, pad)
            _taktik_cache["board"] = {
                "board": render_board(Board("de_dust2", places=BOARD_PLACES.get("de_dust2", {}),
                                            target_places=[]), width=width, pad=pad, theme=ARENA),
                "proj": {"minx": proj.minx, "maxy": proj.maxy, "s": proj.s, "pad": proj.pad,
                         "w": width, "h": proj.height}}
        return _taktik_cache["board"]

    @app.get("/arena/taktik", response_class=HTMLResponse)
    def arena_taktik_page(request: Request):
        from squadmind.analysis import replay as rp
        user = current_user(request)
        if user is None:
            return RedirectResponse("/", status_code=303)
        try:
            tbd = taktik_board()
            payload = {"proj": tbd["proj"], "players": builder_players(user, "de_dust2"),
                       "levels": level_options(), "spawns": rp.spawns(), "ct_holds": rp.default_ct_holds(),
                       "templates": rp.templates(), "label": rp.FLOW_LABEL, "me": user.steam_id}
        except Exception:  # noqa: BLE001
            log.exception("taktik modu kurulamadı")
            return render_report(request, "Arena · Taktik", "_Bu harita için harita geometrisi yok._")
        return page(request, "arena_taktik.html", user=user, board=tbd["board"], payload=payload,
                    label=rp.FLOW_LABEL,
                    w=tbd["proj"]["w"], h=tbd["proj"]["h"])

    def _points(raw: Any, limit: int) -> list[tuple[float, float]]:
        """Gövdedeki [[x, y], ...] listesini doğrula (sayı, sınırlı, haritanın içinde)."""
        from squadmind.analysis import replay as rp
        if not isinstance(raw, list) or len(raw) > limit:
            raise HTTPException(400, "Nokta listesi geçersiz.")
        out = []
        for p in raw:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError, KeyError):
                raise HTTPException(400, "Nokta geçersiz.") from None
            if not (abs(x) <= rp.MAX_COORD and abs(y) <= rp.MAX_COORD):   # NaN de burada düşer
                raise HTTPException(400, "Nokta haritanın dışında.")
            out.append((x, y))
        return out

    @app.post("/api/arena/taktik/rota")
    async def arena_taktik_route(request: Request):
        """Rota önizlemesi: tıklanan noktalar yürünebilir alana yapışır, yol navmesh'te çizilir.

        Gövde: {slot: 0-4, points: [[x, y], ...]} (T) ya da {side: "ct", points: [[x, y]]} (tutma noktası).
        """
        from squadmind.analysis import replay as rp
        from starlette.concurrency import run_in_threadpool
        require_user(request)
        body = await request.json()
        try:
            if body.get("side") == "ct":
                pts = _points(body.get("points"), 1)
                if not pts:
                    raise HTTPException(400, "Nokta yok.")
                x, y, _ = rp.snap("de_dust2", *pts[0])
                return {"points": [[round(x, 1), round(y, 1)]]}
            slot = body.get("slot")
            if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < 5:
                raise HTTPException(400, "Oyuncu sırası geçersiz.")
            pts = _points(body.get("points"), rp.MAX_WAYPOINTS)
            snapped, path = await run_in_threadpool(rp.resolve_route, "de_dust2", rp.spawns()[slot], pts)
        except rp.RouteError as e:
            raise HTTPException(400, str(e)) from None
        return {"points": [[round(x, 1), round(y, 1)] for x, y in snapped],
                "path": [[round(x, 1), round(y, 1)] for x, y in path]}

    @app.post("/api/arena/taktik/akis")
    async def arena_taktik_flow(request: Request):
        """Tek örnek akış. Gövde: {seed, t: [5 × {steam_id | level, weapon, points, delay, plant}],
        ct: [5 × {steam_id | level, weapon, point}]}. Yanıtta olasılık / skor / kazanan YOK."""
        from squadmind.analysis import replay as rp
        from starlette.concurrency import run_in_threadpool

        user = require_user(request)
        body = await request.json()
        t_raw, ct_raw = body.get("t"), body.get("ct")
        if not (isinstance(t_raw, list) and isinstance(ct_raw, list) and len(t_raw) == 5 and len(ct_raw) == 5
                and all(isinstance(u, dict) for u in t_raw + ct_raw)):
            raise HTTPException(400, "Her takımda 5 oyuncu olmalı.")
        seed = body.get("seed", 0)
        if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2 ** 31:
            raise HTTPException(400, "Tohum geçersiz.")
        # İzin: kullanılan her oyuncu izinli listede olmalı (veri hiçbir şey hesaplanmadan önce)
        players = builder_players(user, "de_dust2")
        by_sid = {p["steam_id"]: p for p in players}
        if any(u.get("steam_id") and u["steam_id"] not in by_sid for u in t_raw + ct_raw):
            raise HTTPException(403, "Bu oyuncunun verisini kullanma iznin yok.")
        levels = {lv["level"]: lv["elo"] for lv in level_options()}
        if any(u.get("weapon", "rifle") not in rp.CLASSES for u in t_raw + ct_raw):
            raise HTTPException(400, "Silah geçersiz.")
        if any(u.get("steam_id") for u in t_raw + ct_raw):
            builder_ratings("de_dust2", players)        # ELO canlı FACEIT'ten (önbellekli)

        def strength(u: dict) -> tuple[str, float]:
            sid = u.get("steam_id")
            if sid:
                f = _rating_cache["feats0"].get(sid)
                if f is None:
                    raise HTTPException(503, "Güç puanı şu an hesaplanamadı (FACEIT).")
                return by_sid[sid]["nickname"], float(f.combined_elo)
            lv = u.get("level", 9)
            if lv not in levels:
                raise HTTPException(400, "Seviye geçersiz.")
            return f"Ort. L{lv}", float(levels[lv])

        t_slots, ct_slots = [], []
        for u in t_raw:
            name, elo = strength(u)
            try:
                delay = min(10.0, max(0.0, float(u.get("delay", 0))))
            except (TypeError, ValueError):
                raise HTTPException(400, "Gecikme geçersiz.") from None
            t_slots.append(rp.Slot(name, elo, u.get("weapon", "rifle"),
                                   tuple(_points(u.get("points", []), rp.MAX_WAYPOINTS)), delay,
                                   bool(u.get("plant"))))
        for u in ct_raw:
            name, elo = strength(u)
            pt = _points(u.get("point") and [u["point"]], 1)
            if not pt:
                raise HTTPException(400, "CT tutma noktası eksik.")
            ct_slots.append(rp.Slot(name, elo, u.get("weapon", "rifle"), (pt[0],)))
        if sum(s.plant for s in t_slots) > 1:
            raise HTTPException(400, "Bombayı en çok bir oyuncu taşır.")

        def run_flow() -> dict:
            scn, _ = rp.build_scenario(t_slots, ct_slots)
            return rp.replay(rp.prepare_cached(scn), seed)

        try:
            flow = await run_in_threadpool(run_flow)
        except rp.RouteError as e:
            raise HTTPException(400, str(e)) from None
        flow["seed"], flow["label"] = seed, rp.FLOW_LABEL
        return flow

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


def make_login_link(secret: str, base_url: str, steam_id: str, faceit_id: str = "",
                    nickname: str = "") -> str:
    """Yönetici giriş linki (bkz. /giris/link). faceit_id + nickname verilirse kayıtsız oyuncu da girer."""
    payload: str | dict = ({"s": steam_id, "f": faceit_id, "n": nickname}
                           if faceit_id and nickname else steam_id)
    token = URLSafeTimedSerializer(secret, salt=LINK_SALT).dumps(payload)
    return f"{base_url.rstrip('/')}/giris/link?t={token}"


def default_secret(secret: str) -> str:
    """Ayarlı gizli anahtar; yoksa rastgele (sunucu yeniden başlayınca oturumlar düşer)."""
    if secret:
        return secret
    log.warning("WEB_SECRET yok: rastgele anahtar kullanılıyor, yeniden başlatınca oturumlar düşer")
    return secrets.token_urlsafe(32)
