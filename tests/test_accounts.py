"""Hesaplar: e-posta kaydı / girişi, FACEIT bağlantısı, eski oturum geçişi, CSRF, hız sınırı, e-posta.

FACEIT ve SMTP sahte; gerçek ağ isteği yok (test_web ile aynı kurgu).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from squadmind.db import connect, init_db
from squadmind.web import accounts as acc
from squadmind.web.app import WebSettings, create_app
from squadmind.web.mailer import MailError
from squadmind.web.store import Store
from squadmind.web.worker import Worker
from tests.test_ingest_demos import SELF

INVITE = "takim-davet-123"
PW = "uzun-bir-sifre-1"


class FakeMailer:
    """SMTP'ye gitmeyen posta kutusu; gönderilenler `sent` listesinde."""
    configured = True

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def send(self, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise MailError("smtp")
        self.sent.append((to, subject, body))

    def last_token(self) -> str:
        return re.search(r"t=(\S+)", self.sent[-1][2]).group(1)


class FakeOAuth:
    """FACEIT'e gitmeyen OAuth; kullanıcı bilgisine e-posta konabilir."""
    configured = True

    def __init__(self) -> None:
        self.email: str | None = None
        self.email_verified: bool | None = None

    def authorize_url(self, state, challenge):
        return f"https://accounts.faceit.com/accounts?state={state}"

    def fetch_user(self, code, verifier):
        user: dict = {"guid": "p-self"}
        if self.email:
            user["email"] = self.email
        if self.email_verified is not None:
            user["email_verified"] = self.email_verified
        return user


def build(tmp_path, make_client, mailer=None, oauth=None, limiter=None):
    store = Store(tmp_path / "web.duckdb")
    worker = Worker(store, tmp_path / "processed", tmp_path / "tmp", client_factory=make_client)
    worker.submit = lambda job: None
    settings = WebSettings(password_login=True, invite_code=INVITE, secret="test-secret", required_demos=10,
                           team_owner=None, map_name=None, uploads_dir=tmp_path / "uploads",
                           oauth=oauth, mailer=mailer, base_url="https://app.test",
                           login_limiter=limiter)
    client = TestClient(create_app(settings, store, worker, client_factory=make_client))
    return client, store


@pytest.fixture
def site(tmp_path, make_client):
    mailer, oauth = FakeMailer(), FakeOAuth()
    client, store = build(tmp_path, make_client, mailer, oauth)
    yield client, store, mailer, oauth
    store.close()


@pytest.fixture
def site_nomail(tmp_path, make_client):
    client, store = build(tmp_path, make_client)
    yield client, store
    store.close()


def csrf_of(client, path: str) -> str:
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def register(client, email="Oyuncu@Example.com", password=PW, nickname="TestNick", davet=INVITE,
             csrf=None):
    token = csrf if csrf is not None else csrf_of(client, "/kayit")
    return client.post("/kayit", data={"email": email, "password": password, "nickname": nickname,
                                       "davet": davet, "csrf": token}, follow_redirects=False)


def login(client, email, password, csrf=None):
    token = csrf if csrf is not None else csrf_of(client, "/giris")
    return client.post("/giris", data={"email": email, "password": password, "csrf": token},
                       follow_redirects=False)


def faceit_login(client):
    r = client.get(f"/giris/faceit?davet={INVITE}", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    return client.get(f"/giris/faceit/donus?code=kod&state={state}", follow_redirects=False)


# --- kayıt ---------------------------------------------------------------------------

def test_register_creates_account_linked_unverified(site):
    client, store, mailer, _ = site
    r = register(client)
    assert r.status_code == 303 and r.headers["location"] == "/ben"
    account, link = acc.account_of(store, SELF)
    assert account.email == "oyuncu@example.com"           # küçük harf
    assert account.password_hash.startswith("$argon2") and PW not in account.password_hash
    assert link == acc.UNVERIFIED and not account.email_verified
    assert store.user(SELF).team_access                    # davetle girdi: mevcut katılım kuralı
    assert mailer.sent and mailer.sent[0][0] == "oyuncu@example.com"
    page = client.get("/hesap").text
    assert "oyuncu@example.com" in page and "Doğrulanmadı" in page and "TestNick" in page


def test_register_rejections(site):
    client, store, *_ = site
    assert "en az 10" in register(client, password="kisa").text
    assert "Geçerli bir e-posta" in register(client, email="degil").text
    assert "Davet kodu geçersiz" in register(client, davet="yanlis").text
    assert "bulunamadı" in register(client, nickname="kimse").text
    assert store.users() == [] and acc.by_email(store, "oyuncu@example.com") is None
    assert register(client).status_code == 303
    # aynı e-posta / aynı FACEIT ikinci kez açılamaz
    other = TestClient(client.app)
    assert "zaten bir hesap var" in register(other).text
    assert "zaten bir hesaba bağlı" in register(other, email="baska@example.com").text


def test_register_requires_csrf(site):
    client, store, *_ = site
    assert register(client, csrf="sahte").status_code == 400
    bare = TestClient(client.app)  # çerezsiz: imzalı token tek başına yetmez
    token = csrf_of(client, "/kayit")
    assert register(bare, csrf=token).status_code == 400
    assert store.users() == []


# --- giriş ------------------------------------------------------------------------------

def test_login_right_wrong_and_unknown(site):
    client, store, *_ = site
    register(client)
    fresh = TestClient(client.app)
    r = login(fresh, "OYUNCU@example.com ", PW)
    assert r.status_code == 303 and r.headers["location"] == "/ben"
    assert acc.account_of(store, SELF)[0].last_login_at is not None
    assert fresh.get("/hesap").status_code == 200
    other = TestClient(client.app)
    wrong = login(other, "oyuncu@example.com", "yanlis-sifre-123")
    unknown = login(other, "yok@example.com", "yanlis-sifre-123")
    assert wrong.status_code == unknown.status_code == 400
    assert "E-posta ya da şifre hatalı" in wrong.text
    assert "E-posta ya da şifre hatalı" in unknown.text    # hesap varlığı sızmaz
    assert login(other, "oyuncu@example.com", PW, csrf="sahte").status_code == 400


def test_login_rate_limit(tmp_path, make_client):
    limiter = acc.LoginLimiter(limit=5, window=900)
    client, store = build(tmp_path, make_client, limiter=limiter)
    register(client)
    other = TestClient(client.app)
    token = csrf_of(other, "/giris")
    for _ in range(5):
        assert login(other, "oyuncu@example.com", "yanlis-sifre-123", token).status_code == 400
    blocked = login(other, "oyuncu@example.com", PW, token)    # doğru şifre bile kilitli
    assert blocked.status_code == 429 and "Çok fazla" in blocked.text
    store.close()


def test_limiter_window_expires():
    now = [0.0]
    lim = acc.LoginLimiter(limit=2, window=10, clock=lambda: now[0])
    lim.fail("a", "ip"), lim.fail("a", "ip")
    assert lim.blocked("a", "ip")
    now[0] = 11
    assert not lim.blocked("a", "ip")


def test_scrypt_fallback_roundtrip():
    stored = "scrypt$" + __import__("base64").b64encode(b"s" * 16).decode() + "$" + \
        __import__("base64").b64encode(acc._scrypt(PW, b"s" * 16)).decode()
    assert acc.verify_password(PW, stored) and not acc.verify_password("baska", stored)
    assert not acc.verify_password(PW, None) and not acc.verify_password(PW, "bozuk")


# --- eski oturum -> e-posta ekle ----------------------------------------------------------

def test_legacy_cookie_session_is_prompted_to_add_email(site):
    client, store, *_ = site
    r = client.post("/katil", data={"nickname": "TestNick", "davet": INVITE}, follow_redirects=False)
    assert r.status_code == 303                                  # eski çerez: steam_id'li
    assert acc.account_of(store, SELF) is None
    to = client.get("/ben", follow_redirects=False)
    assert to.status_code == 303 and to.headers["location"] == "/hesap/eposta"
    token = csrf_of(client, "/hesap/eposta")
    done = client.post("/hesap/eposta", data={"email": "Eski@Example.com", "password": PW,
                                              "csrf": token}, follow_redirects=False)
    assert done.status_code == 303
    account, link = acc.account_of(store, SELF)
    assert account.email == "eski@example.com" and link == acc.UNVERIFIED
    assert store.user(SELF).nickname == "TestNick"               # veri kaybı yok
    assert client.get("/ben", follow_redirects=False).status_code == 200
    # ertelenmiş: "şimdilik geç" çerezi /ben'i açar
    legacy = TestClient(client.app)
    legacy.cookies.set("squadmind_oturum", client.cookies.get("squadmind_oturum"))
    assert legacy.get("/ben", follow_redirects=False).status_code == 200


def test_legacy_skip_cookie(site):
    client, *_ = site
    client.post("/katil", data={"nickname": "TestNick", "davet": INVITE})
    assert client.get("/hesap/atla", follow_redirects=False).status_code == 303
    assert client.get("/ben", follow_redirects=False).status_code == 200


def test_add_email_rejects_taken_email(site):
    client, store, *_ = site
    acc.create(store, "dolu@example.com", acc.hash_password(PW), True)
    client.post("/katil", data={"nickname": "TestNick", "davet": INVITE})
    token = csrf_of(client, "/hesap/eposta")
    r = client.post("/hesap/eposta", data={"email": "dolu@example.com", "password": PW,
                                           "csrf": token})
    assert "zaten bir hesap var" in r.text and acc.account_of(store, SELF) is None


# --- FACEIT ile giriş ---------------------------------------------------------------------

def test_faceit_login_auto_creates_verified_account(site):
    client, store, _, oauth = site
    oauth.email = "Faceit@Example.com"
    assert faceit_login(client).headers["location"] == "/ben"
    account, link = acc.account_of(store, SELF)
    assert account.email == "faceit@example.com" and account.email_verified
    assert account.password_hash is None and link == acc.VERIFIED
    again = TestClient(client.app)
    oauth_state = faceit_login(again)                            # ikinci giriş aynı hesabı bulur
    assert oauth_state.status_code == 303
    assert store.run(lambda con: con.execute("SELECT count(*) FROM accounts").fetchone()[0]) == 1
    assert "FACEIT doğrulandı" in client.get("/hesap").text


def test_faceit_login_without_email_creates_emailless_account(site):
    client, store, *_ = site
    faceit_login(client)
    account, link = acc.account_of(store, SELF)
    assert account.email is None and link == acc.VERIFIED
    to = client.get("/hesap", follow_redirects=False)            # e-posta eklemesi istenir
    assert to.status_code == 200 and "E-posta ekle" in to.text


def test_faceit_login_binds_existing_unverified_account_and_drops_password(site):
    client, store, _, oauth = site
    acc.create(store, "kurban@example.com", acc.hash_password("saldirgan-sifre-1"), False)
    oauth.email = "kurban@example.com"
    faceit_login(client)
    account, link = acc.account_of(store, SELF)
    assert account.email == "kurban@example.com" and link == acc.VERIFIED and account.email_verified
    assert account.password_hash is None        # önceden kaydedilen şifre geçersiz (ön-ele geçirme)


def test_faceit_login_keeps_password_of_verified_account(site):
    client, store, _, oauth = site
    account_id = acc.create(store, "sahip@example.com", acc.hash_password(PW), True)
    oauth.email = "sahip@example.com"
    faceit_login(client)
    account, link = acc.account_of(store, SELF)
    assert account.account_id == account_id and link == acc.VERIFIED
    assert acc.verify_password(PW, account.password_hash)


def test_nickname_registration_becomes_verified_after_faceit_login(site):
    client, store, _, oauth = site
    register(client)
    assert acc.account_of(store, SELF)[1] == acc.UNVERIFIED
    oauth.email = "oyuncu@example.com"
    other = TestClient(client.app)
    faceit_login(other)
    account, link = acc.account_of(store, SELF)
    assert link == acc.VERIFIED and account.email_verified
    assert store.run(lambda con: con.execute("SELECT count(*) FROM accounts").fetchone()[0]) == 1


def test_one_faceit_cannot_link_two_accounts_and_one_account_one_faceit(site):
    client, store, *_ = site
    register(client)
    second = acc.create(store, "ikinci@example.com", acc.hash_password(PW), True)
    with pytest.raises(acc.AccountError, match="başka bir hesaba bağlı"):
        acc.link_user(store, second, SELF, acc.VERIFIED)
    first, _ = acc.account_of(store, SELF)
    store.add_user("s-baska", "p-baska", "Baska")
    with pytest.raises(acc.AccountError, match="başka bir FACEIT"):
        acc.link_user(store, first.account_id, "s-baska", acc.VERIFIED)


def test_faceit_email_in_use_by_other_linked_account_is_not_reused(site):
    client, store, _, oauth = site
    store.add_user("s-baska", "p-baska", "Baska")
    first = acc.create(store, "ortak@example.com", None, True)
    acc.link_user(store, first, "s-baska", acc.VERIFIED)
    oauth.email = "ortak@example.com"
    faceit_login(client)
    account, link = acc.account_of(store, SELF)
    assert account.account_id != first and account.email is None and link == acc.VERIFIED


# --- e-posta kapalı / açık --------------------------------------------------------------------

def test_mail_endpoints_hidden_without_mailer(site_nomail):
    client, store = site_nomail
    assert client.get("/sifre-unuttum").status_code == 404
    assert client.post("/sifre-unuttum", data={"email": "a@b.co"}).status_code == 404
    assert client.get("/sifre-sifirla?t=x").status_code == 404
    assert client.get("/hesap/dogrula?t=x").status_code == 404
    assert "Şifremi unuttum" not in client.get("/giris").text
    register(client)
    assert client.post("/hesap/dogrulama-gonder", data={"csrf": "x"}).status_code == 404
    assert "yeniden yolla" not in client.get("/hesap").text
    assert acc.account_of(store, SELF)[0].email == "oyuncu@example.com"   # kayıt yine çalışır


def test_verification_link_marks_email_verified(site):
    client, store, mailer, _ = site
    register(client)
    token = mailer.last_token()
    assert client.get("/hesap/dogrula?t=bozuk").status_code == 400
    r = client.get(f"/hesap/dogrula?t={token}", follow_redirects=False)
    assert r.status_code == 303 and acc.account_of(store, SELF)[0].email_verified


def test_forgot_password_flow_is_single_use(site):
    client, store, mailer, _ = site
    register(client)
    mailer.sent.clear()
    anon = TestClient(client.app)
    assert "Şifremi unuttum" in anon.get("/giris").text
    token = csrf_of(anon, "/sifre-unuttum")
    unknown = anon.post("/sifre-unuttum", data={"email": "yok@example.com", "csrf": token})
    known = anon.post("/sifre-unuttum", data={"email": "oyuncu@example.com", "csrf": token})
    assert "sıfırlama bağlantısı yollandı" in unknown.text and "sıfırlama bağlantısı yollandı" in known.text
    assert len(mailer.sent) == 1 and mailer.sent[0][0] == "oyuncu@example.com"
    link_token = mailer.last_token()
    form = anon.get(f"/sifre-sifirla?t={link_token}")
    assert form.status_code == 200
    new_pw = "yepyeni-sifre-99"
    r = anon.post("/sifre-sifirla", data={"t": link_token, "password": new_pw,
                                          "csrf": csrf_of(anon, f"/sifre-sifirla?t={link_token}")},
                  follow_redirects=False)
    assert r.status_code == 303
    assert login(TestClient(client.app), "oyuncu@example.com", new_pw).status_code == 303
    assert login(TestClient(client.app), "oyuncu@example.com", PW).status_code == 400
    # aynı bağlantı ikinci kez kullanılamaz
    assert anon.get(f"/sifre-sifirla?t={link_token}").status_code == 400
    assert acc.account_of(store, SELF)[0].email_verified


def test_reset_weak_password_and_csrf(site):
    client, _, mailer, _ = site
    register(client)
    anon = TestClient(client.app)
    token = csrf_of(anon, "/sifre-unuttum")
    anon.post("/sifre-unuttum", data={"email": "oyuncu@example.com", "csrf": token})
    t = mailer.last_token()
    assert anon.post("/sifre-sifirla", data={"t": t, "password": "yeni-sifre-99",
                                             "csrf": "sahte"}).status_code == 400
    assert "en az 10" in anon.post("/sifre-sifirla", data={"t": t, "password": "kisa", "csrf": token}).text


def test_mail_failure_does_not_break_registration(tmp_path, make_client):
    client, store = build(tmp_path, make_client, mailer=FakeMailer(fail=True))
    assert register(client).status_code == 303
    assert acc.account_of(store, SELF) is not None
    store.close()


# --- hesap sayfası ---------------------------------------------------------------------------------

def test_change_password(site):
    client, store, *_ = site
    register(client)
    token = csrf_of(client, "/hesap")
    bad = client.post("/hesap/sifre", data={"current": "yanlis-yanlis-1", "password": "yepyeni-sifre-99",
                                            "csrf": token})
    assert "Mevcut şifre hatalı" in bad.text
    weak = client.post("/hesap/sifre", data={"current": PW, "password": "kisa", "csrf": token})
    assert "en az 10" in weak.text
    assert client.post("/hesap/sifre", data={"current": PW, "password": "yepyeni-sifre-99",
                                             "csrf": "sahte"}).status_code == 400
    ok = client.post("/hesap/sifre", data={"current": PW, "password": "yepyeni-sifre-99",
                                           "csrf": token}, follow_redirects=False)
    assert ok.status_code == 303
    assert acc.verify_password("yepyeni-sifre-99", acc.account_of(store, SELF)[0].password_hash)


def test_account_page_requires_login(site):
    client, *_ = site
    r = client.get("/hesap", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/giris"


def test_privacy_mentions_email_and_hash(site):
    client, *_ = site
    page = client.get("/privacy").text
    assert "karması" in page and "e-posta" in page.lower()


# --- şema ---------------------------------------------------------------------------------------------

def test_schema_v11_migration_is_idempotent():
    con = connect(":memory:")
    init_db(con)
    con.execute("DELETE FROM schema_version")
    con.execute("INSERT INTO schema_version VALUES (10)")
    init_db(con)
    init_db(con)
    assert con.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 12
    cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'web_users'").fetchall()}
    assert {"account_id", "faceit_link"} <= cols
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert "accounts" in tables
    con.close()


def test_list_accounts_for_cli(site):
    client, store, *_ = site
    register(client)
    rows = store.run(lambda con: acc.list_accounts(con))
    assert rows[0][0] == "oyuncu@example.com" and rows[0][2] == "TestNick" and rows[0][3] == acc.UNVERIFIED


# --- şifre değişince eski oturumlar düşer; zayıf şifreler reddedilir ---------------------------

def test_password_change_logs_out_other_sessions(site):
    client, store, *_ = site
    r = client.post("/katil", data={"nickname": "TestNick", "davet": INVITE}, follow_redirects=False)
    token = csrf_of(client, "/hesap/eposta")
    client.post("/hesap/eposta", data={"email": "s@example.com", "password": PW, "csrf": token})
    assert client.get("/ben", follow_redirects=False).status_code == 200
    other = TestClient(client.app)                       # aynı çerezle ikinci cihaz
    other.cookies.set("squadmind_oturum", client.cookies.get("squadmind_oturum"))
    assert other.get("/ben", follow_redirects=False).status_code == 200
    token = csrf_of(client, "/hesap")
    r = client.post("/hesap/sifre", data={"current": PW, "password": "yeni-Sifre-2026x", "csrf": token},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/ben", follow_redirects=False).status_code == 200      # bu cihaz kalır
    assert other.get("/ben", follow_redirects=False).status_code == 303       # diğeri düşer


@pytest.mark.parametrize("weak", ["aaaaaaaaaaaa", "abababababab", "1234567890", "Qwertyuiop123",
                                  "counterstrike1", "Galatasaray!"])
def test_weak_passwords_rejected(weak):
    with pytest.raises(acc.AccountError):
        acc.check_password_policy(weak, "x@example.com")
