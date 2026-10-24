"""Yalnız FACEIT ile giriş (varsayılan, WEB_PASSWORD_LOGIN kapalı): eski rotalar kapalı,
davetsiz FACEIT girişi hesap açar, yedek link kayıtsız oyuncuyu da içeri alır.

FACEIT sahte (test_accounts ile aynı kurgu); gerçek ağ isteği yok.
"""

from __future__ import annotations

import pytest
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from squadmind.web import accounts as acc
from squadmind.web.app import LINK_SALT, WebSettings, create_app, make_login_link
from squadmind.web.store import Store
from squadmind.web.worker import Worker
from tests.test_accounts import FakeMailer, FakeOAuth
from tests.test_ingest_demos import SELF


def build(tmp_path, make_client, password_login=False):
    store = Store(tmp_path / "web.duckdb")
    worker = Worker(store, tmp_path / "processed", tmp_path / "tmp", client_factory=make_client)
    worker.submit = lambda job: None
    oauth = FakeOAuth()
    settings = WebSettings(invite_code="davet-x", secret="test-secret", required_demos=10,
                           team_owner=None, map_name=None, uploads_dir=tmp_path / "uploads",
                           oauth=oauth, mailer=FakeMailer(), base_url="https://app.test",
                           password_login=password_login)
    client = TestClient(create_app(settings, store, worker, client_factory=make_client))
    return client, store, oauth


@pytest.fixture
def site(tmp_path, make_client):
    client, store, oauth = build(tmp_path, make_client)
    yield client, store, oauth
    store.close()


def faceit_login(client, davet=""):
    """Davetsiz FACEIT girişi: /giris/faceit -> sahte FACEIT dönüşü."""
    r = client.get(f"/giris/faceit{'?davet=' + davet if davet else ''}", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    return client.get(f"/giris/faceit/donus?code=kod&state={state}", follow_redirects=False)


def test_old_routes_redirect_or_404_when_closed(site):
    client, store, _ = site
    for path in ("/giris", "/kayit", "/sifre-unuttum", "/sifre-sifirla?t=x", "/hesap/eposta",
                 "/hesap/dogrula?t=x"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", path
    for path in ("/katil", "/kayit", "/giris", "/sifre-unuttum", "/sifre-sifirla", "/hesap/eposta",
                 "/hesap/sifre", "/hesap/dogrulama-gonder"):
        assert client.post(path, data={"nickname": "x", "davet": "davet-x"}).status_code == 404, path
    assert store.users() == []


def test_home_shows_only_faceit_button(site):
    client, *_ = site
    html = client.get("/").text
    assert 'href="/giris/faceit' in html
    for gone in ('action="/katil"', 'href="/giris"', 'href="/kayit', "Nickname ile katıl"):
        assert gone not in html
    # davet linkiyle gelen de nickname formu görmez
    assert 'action="/katil"' not in client.get("/?davet=davet-x").text


def test_faceit_login_without_invite_creates_account_and_reuses_it(site):
    client, store, oauth = site
    oauth.email = "Yeni@Example.com"
    r = faceit_login(client)                                   # davetsiz, sahiple maçı yok
    assert r.headers["location"] == "/ben"
    assert store.user(SELF) is not None and not store.user(SELF).team_access
    account, link = acc.account_of(store, SELF)
    assert link == acc.VERIFIED and account.email == "yeni@example.com"
    assert client.get("/ben").status_code == 200
    again = TestClient(client.app)                             # ikinci giriş aynı hesaba
    assert faceit_login(again).headers["location"] == "/ben"
    assert acc.account_of(store, SELF)[0].account_id == account.account_id
    assert store.run(lambda con: con.execute("SELECT count(*) FROM accounts").fetchone()[0]) == 1
    page = again.get("/hesap").text                            # şifre bölümü gizli
    assert "TestNick" in page and "Şifre belirle" not in page and "/hesap/sifre" not in page


def test_account_page_and_me_work_without_email_flow(site):
    client, store, _ = site
    faceit_login(client)
    assert "/hesap/eposta" not in client.get("/ben").text
    assert client.get("/hesap", follow_redirects=False).status_code == 200


def test_legacy_session_without_account_gets_one_silently(site):
    client, store, _ = site
    faceit_login(client)
    store.run(lambda con: con.execute("UPDATE web_users SET account_id = NULL"))
    store.run(lambda con: con.execute("DELETE FROM accounts"))
    assert client.get("/ben", follow_redirects=False).status_code == 200
    assert acc.account_of(store, SELF) is not None


def test_backup_link_creates_unregistered_player(site):
    client, store, _ = site
    token = make_login_link("test-secret", "http://x", SELF, "p-self", "TestNick").split("?t=")[1]
    assert store.user(SELF) is None
    r = client.get(f"/giris/link?t={token}", follow_redirects=False)
    assert r.headers["location"] == "/ben" and "squadmind_oturum" in r.headers["set-cookie"]
    assert store.user(SELF).nickname == "TestNick"
    account, link = acc.account_of(store, SELF)
    assert account.email is None and link == acc.UNVERIFIED
    assert client.get("/ben").status_code == 200


def test_backup_link_old_payload_still_works_for_known_user_only(site):
    client, store, _ = site
    old = URLSafeTimedSerializer("test-secret", salt=LINK_SALT).dumps(SELF)
    assert "kayıtlı değil" in client.get(f"/giris/link?t={old}").text      # kayıtsız: eski biçim açmaz
    faceit_login(client)
    client.cookies.clear()
    r = client.get(f"/giris/link?t={old}", follow_redirects=False)
    assert r.headers["location"] == "/ben"
    assert "süresi dolmuş" in client.get("/giris/link?t=sahte").text
    bad = make_login_link("baska", "http://x", SELF, "p-self", "TestNick").split("?t=")[1]
    assert "süresi dolmuş" in client.get(f"/giris/link?t={bad}").text


def test_password_login_flag_restores_old_behaviour(tmp_path, make_client):
    client, store, _ = build(tmp_path, make_client, password_login=True)
    try:
        assert client.get("/giris").status_code == 200
        assert client.get("/kayit").status_code == 200
        assert 'href="/giris"' in client.get("/").text
        r = client.post("/katil", data={"nickname": "TestNick", "davet": "davet-x"},
                        follow_redirects=False)
        assert r.status_code == 303 and store.user(SELF) is not None
    finally:
        store.close()


def test_popup_login_returns_bridge_page_with_cookie(site):
    """Açılır pencereden başlayan FACEIT girişi: 200 köprü sayfası + oturum çerezi, ana pencereye yönlendirir."""
    client = site[0]
    start = client.get("/giris/faceit?popup=1", follow_redirects=False)
    assert start.status_code == 303
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    back = client.get(f"/giris/faceit/donus?code=kod&state={state}", follow_redirects=False)
    assert back.status_code == 200 and "window.opener" in back.text and '"/ben"' in back.text
    assert "squadmind_oturum" in back.headers.get("set-cookie", "")
