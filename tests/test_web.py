"""Web arayüzü testleri: davetle katılım, parça parça yükleme, kopya reddi, yetki, raporlar.

FACEIT sahte istemciyle (conftest), demo parse sahte tablolarla (test_ingest_demos);
işçi iş parçacığı başlatılmaz, işler `submit` anında çalışır.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from squadmind.web.app import WebSettings, create_app, safe_file_name
from squadmind.web.store import Store
from squadmind.web.worker import Worker
from tests.test_ingest_demos import M1, SELF, make_tables

INVITE = "takim-davet-123"


@pytest.fixture
def web(tmp_path, make_client):
    store = Store(tmp_path / "web.duckdb")
    worker = Worker(store, tmp_path / "processed", tmp_path / "tmp",
                    client_factory=make_client, parser=lambda p: make_tables())

    def run_now(job):
        worker.handle(job)
        if job.kind == "sync":  # fixture maç ID'si "m-1": gerçek FACEIT biçimine çevir
            store.run(lambda con: [con.execute(
                f"UPDATE {t} SET match_id = ? WHERE match_id = 'm-1'", [M1])
                for t in ("matches", "match_player_stats")])

    worker.submit = run_now
    settings = WebSettings(invite_code=INVITE, secret="test-secret", required_demos=10,
                           team_owner=None, map_name=None, uploads_dir=tmp_path / "uploads")
    app = create_app(settings, store, worker, client_factory=make_client)
    app.state.worker = worker  # testler takım sahibini değiştirebilsin
    client = TestClient(app)
    yield client, store
    store.close()


def _join(client):
    return client.post("/katil", data={"nickname": "TestNick", "davet": INVITE},
                       follow_redirects=False)


def _upload(client, name: str, data: bytes, declared: int | None = None):
    r = client.post("/api/yukleme", json={"name": name, "size": declared or len(data)})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    assert client.put(f"/api/yukleme/{uid}/0", content=data).status_code == 200
    return uid, client.post(f"/api/yukleme/{uid}/bitti")


def test_join_requires_valid_invite(web):
    client, store = web
    r = client.post("/katil", data={"nickname": "TestNick", "davet": "yanlis"})
    assert "Davet kodu geçersiz" in r.text
    assert store.users() == []


def test_join_unknown_nickname(web):
    client, _ = web
    r = client.post("/katil", data={"nickname": "kimse", "davet": INVITE})
    assert "bulunamadı" in r.text


def test_join_then_upload_and_ingest(web):
    client, store = web
    r = _join(client)
    assert r.status_code == 303 and r.headers["location"] == "/ben"
    user = store.user(SELF)
    assert user.nickname == "TestNick" and user.sync_status.startswith("tamam")

    uid, done = _upload(client, f"{M1}-1-1.dem", b"demo-bytes")
    assert done.status_code == 200
    up = store.upload(uid)
    assert (up["status"], up["match_id"]) == ("tamam", M1)
    assert store.done_count(SELF) == 1

    page = client.get("/ben").text
    assert "1 / 10" in page and f"{M1}-1-1.dem" in page
    assert client.get("/rapor/ben").status_code == 200


def test_duplicate_upload_is_rejected(web):
    client, store = web
    _join(client)
    _upload(client, f"{M1}-1-1.dem", b"demo-bytes")
    uid, _ = _upload(client, "kopya.dem", b"demo-bytes")  # aynı içerik, farklı ad
    up = store.upload(uid)
    assert up["status"] == "reddedildi" and "daha önce eklendi" in up["message"]


def test_incomplete_upload_is_not_processed(web):
    client, store = web
    _join(client)
    uid, done = _upload(client, f"{M1}-1-1.dem", b"yarim", declared=10)
    assert done.status_code == 400
    assert store.upload(uid)["status"] == "hata"


def test_bad_file_type_and_auth(web):
    client, _ = web
    assert client.post("/api/yukleme", json={"name": "a.dem", "size": 10}).status_code == 401
    _join(client)
    r = client.post("/api/yukleme", json={"name": "virus.exe", "size": 10})
    assert r.status_code == 400
    # Başkasının (var olmayan) yüklemesine parça yazılamaz
    assert client.put("/api/yukleme/" + "0" * 32 + "/0", content=b"x").status_code == 404


def test_report_escapes_html(web):
    client, store = web
    _join(client)
    # Rapora giren dış metin (oyuncu adı) HTML olarak çalışmamalı
    store.run(lambda con: con.execute("UPDATE web_users SET nickname = '<script>x</script>'"))
    html = client.get("/rapor/ben").text
    assert "<script>x</script>" not in html


def test_safe_file_name():
    assert safe_file_name("C:\\Users\\a\\Downloads\\1-abc-1-1.dem.zst") == "1-abc-1-1.dem.zst"
    assert safe_file_name("../../etc/passwd") == "passwd"
    assert safe_file_name("maç demo.dem") == "ma__demo.dem"


@pytest.mark.parametrize(("owner", "warned"), [("Mate", False), ("Yabanci", True)])
def test_join_warns_when_no_shared_match_with_owner(web, owner, warned):
    """Benzer adlı başka hesaba katılma: takım sahibiyle ortak maç yoksa uyarı."""
    client, store = web
    client.app.state.worker.team_owner = owner  # fixture maçında TestNick ile Mate birlikte
    _join(client)
    status = store.user(SELF).sync_status
    assert ("uyarı" in status) is warned
    assert ("doğru hesap mı" in client.get("/ben").text) is warned


# --- FACEIT ile giriş ---------------------------------------------------------------

class FakeOAuth:
    """FACEIT'e gitmeyen OAuth: kod ne olursa olsun fixture oyuncusunu döndürür."""
    configured = True

    def __init__(self):
        self.calls = []

    def authorize_url(self, state, challenge):
        return f"https://accounts.faceit.com/accounts?state={state}&code_challenge={challenge}"

    def fetch_user(self, code, verifier):
        self.calls.append((code, verifier))
        return {"guid": "p-self"}


@pytest.fixture
def oauth_web(tmp_path, make_client):
    store = Store(tmp_path / "web.duckdb")
    worker = Worker(store, tmp_path / "processed", tmp_path / "tmp", client_factory=make_client)
    worker.submit = lambda job: None
    oauth = FakeOAuth()
    settings = WebSettings(invite_code=INVITE, secret="test-secret", required_demos=10,
                           team_owner="Sahip", map_name=None, uploads_dir=tmp_path / "uploads",
                           oauth=oauth)
    client = TestClient(create_app(settings, store, worker, client_factory=make_client))
    yield client, store, oauth
    store.close()


def _faceit_login(client, davet=INVITE, state=None):
    r = client.get(f"/giris/faceit?davet={davet}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.faceit.com/")
    real_state = r.headers["location"].split("state=")[1].split("&")[0]
    return client.get(f"/giris/faceit/donus?code=kod&state={state or real_state}",
                      follow_redirects=False)


def test_faceit_login_creates_user_from_faceit_id(oauth_web):
    client, store, oauth = oauth_web
    r = _faceit_login(client)
    assert r.status_code == 303 and r.headers["location"] == "/ben"
    assert [u.nickname for u in store.users()] == ["TestNick"]  # nickname FACEIT'ten
    assert oauth.calls[0][0] == "kod" and len(oauth.calls[0][1]) > 40  # PKCE doğrulayıcı gitti


def test_faceit_login_rejects_bad_state_and_missing_invite(oauth_web):
    client, store, _ = oauth_web
    assert "doğrulanamadı" in _faceit_login(client, state="sahte").text
    assert "davet linki iste" in _faceit_login(client, davet="yanlis").text
    assert store.users() == []


def test_faceit_login_known_user_needs_no_invite(oauth_web):
    client, store, _ = oauth_web
    _faceit_login(client)
    client.cookies.clear()
    r = _faceit_login(client, davet="")
    assert r.status_code == 303 and r.headers["location"] == "/ben"


def test_nickname_join_closed_when_faceit_login_on(oauth_web):
    client, store, _ = oauth_web
    r = client.post("/katil", data={"nickname": "TestNick", "davet": INVITE})
    assert "FACEIT ile giriş yap" in r.text and store.users() == []


def test_pkce_challenge_is_s256():
    import base64
    import hashlib

    from squadmind.web.faceit_auth import pkce_pair
    v, c = pkce_pair()
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def _shared_matches(store, n, same_side=True):
    """TestNick (p-self) ile takım sahibi 'Sahip' arasında n ortak maç."""
    def fill(con):
        con.execute("INSERT INTO players (faceit_id, steam_id, nickname) VALUES ('p-owner', 's-owner', 'Sahip')")
        for i in range(n):
            con.execute("INSERT INTO match_player_stats (match_id, player_id, nickname, faction) "
                        "VALUES (?, 'p-self', 'TestNick', 'faction1'), (?, 'p-owner', 'Sahip', ?)",
                        [f"m{i}", f"m{i}", "faction1" if same_side else "faction2"])
    store.run(fill)


def test_faceit_login_admits_teammate_without_invite(oauth_web):
    client, store, _ = oauth_web
    _shared_matches(store, 3)
    r = _faceit_login(client, davet="")
    assert r.status_code == 303 and r.headers["location"] == "/ben"


def test_faceit_login_rejects_opponent_and_rare_teammate(oauth_web):
    client, store, _ = oauth_web
    _shared_matches(store, 5, same_side=False)   # hep rakip taraftaydı
    assert "en az 3" in _faceit_login(client, davet="").text
    assert store.users() == []


def test_nickname_join_never_uses_teammate_rule(web):
    # Nickname ile kimlik doğrulanmaz: ortak maç olsa bile davet şart
    client, store = web
    r = client.post("/katil", data={"nickname": "TestNick", "davet": ""})
    assert "Davet kodu geçersiz" in r.text
