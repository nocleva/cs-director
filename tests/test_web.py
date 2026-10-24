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
from tests.conftest import skip_email
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
    settings = WebSettings(password_login=True, invite_code=INVITE, secret="test-secret", required_demos=10,
                           team_owner=None, map_name=None, uploads_dir=tmp_path / "uploads")
    app = create_app(settings, store, worker, client_factory=make_client)
    app.state.worker = worker  # testler takım sahibini değiştirebilsin
    client = skip_email(TestClient(app))
    yield client, store
    store.close()


def _join(client):
    return client.post("/katil", data={"nickname": "TestNick", "davet": INVITE},
                       follow_redirects=False)


def _upload(client, name: str, data: bytes, declared: int | None = None, map_name="de_mirage"):
    r = client.post("/api/yukleme", json={"name": name, "size": declared or len(data),
                                          "map": map_name})
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

    page = client.get("/demolar").text
    assert "<b>1</b> / 10 demo" in page and f"{M1}-1-1.dem" in page
    # Genel bakış yükleme göstermez, toplam analiz sayısını gösterir
    overview = client.get("/ben").text
    assert 'data-count="1"' in overview and 'id="drop"' not in overview


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


def test_page_escapes_html(web):
    client, store = web
    _join(client)
    # Sayfaya giren dış metin (oyuncu adı) HTML olarak çalışmamalı
    store.run(lambda con: con.execute("UPDATE web_users SET nickname = '<script>x</script>'"))
    html = client.get("/ben").text
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
    settings = WebSettings(password_login=True, invite_code=INVITE, secret="test-secret", required_demos=10,
                           team_owner="Sahip", map_name=None, uploads_dir=tmp_path / "uploads",
                           oauth=oauth)
    client = skip_email(TestClient(create_app(settings, store, worker, client_factory=make_client)))
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


def test_faceit_login_rejects_bad_state(oauth_web):
    client, store, _ = oauth_web
    assert "doğrulanamadı" in _faceit_login(client, state="sahte").text
    assert store.users() == []


def test_faceit_login_without_invite_gets_in_but_not_owner_pages(oauth_web):
    # FACEIT'le doğrulanan herkes girer (kendi takımını kurar); kadro erişimi verilmez
    client, store, _ = oauth_web
    assert _faceit_login(client, davet="yanlis").headers["location"] == "/ben"
    assert store.users()[0].team_access is False


def test_faceit_login_known_user_needs_no_invite(oauth_web):
    client, store, _ = oauth_web
    _faceit_login(client)
    client.cookies.clear()
    r = _faceit_login(client, davet="")
    assert r.status_code == 303 and r.headers["location"] == "/ben"


def test_nickname_join_with_invite_is_fallback_when_faceit_login_on(oauth_web):
    client, store, _ = oauth_web
    assert "Davet kodu geçersiz" in client.post("/katil", data={"nickname": "TestNick", "davet": ""}).text
    assert "Davet koduyla katıl" in client.get(f"/?davet={INVITE}").text
    r = client.post("/katil", data={"nickname": "TestNick", "davet": INVITE}, follow_redirects=False)
    assert r.headers["location"] == "/ben" and store.users()[0].team_access is True


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
    assert store.users()[0].team_access is True


def test_faceit_login_rejects_opponent_and_rare_teammate(oauth_web):
    client, store, _ = oauth_web
    _shared_matches(store, 5, same_side=False)   # hep rakip taraftaydı
    _faceit_login(client, davet="")
    assert store.users()[0].team_access is False


def test_nickname_join_never_uses_teammate_rule(web):
    # Nickname ile kimlik doğrulanmaz: ortak maç olsa bile davet şart
    client, store = web
    r = client.post("/katil", data={"nickname": "TestNick", "davet": ""})
    assert "Davet kodu geçersiz" in r.text


def test_legal_pages_are_public(web):
    client, _ = web
    for path, text in (("/privacy", "Şifren bize hiç gelmez"), ("/terms", "bağlantılı değildir")):
        r = client.get(path)
        assert r.status_code == 200 and text in r.text



# --- takım oluşturma ----------------------------------------------------------------

def test_team_flow_invite_accept_share(oauth_web, make_client):
    client, store, _ = oauth_web
    _faceit_login(client)                        # TestNick (p-self)
    r = client.post("/takimlar", data={"name": "Beşli"}, follow_redirects=False)
    team_url = r.headers["location"]
    team_id = team_url.rsplit("/", 1)[1]
    assert "Beşli" in client.get("/takimlar").text

    # Başka bir oyuncuyu ekle: daveti kabul edene kadar verisi paylaşılmaz
    store.run(lambda con: con.execute(
        "INSERT INTO players (faceit_id, steam_id, nickname, level) VALUES ('p-mate', 's-mate', 'Mate', 8)"))
    client.post(f"/takimlar/{team_id}/ekle", data={"faceit_id": "p-mate", "nickname": "Mate"})
    page = client.get(team_url).text
    assert "Mate" in page and "Siteye katılmadı" in page
    assert [m["status"] for m in store.team_members(team_id)] == ["üye", "davet"]

    # Mate siteye katılıp daveti kabul eder → paylaşıyor
    store.add_user("s-mate", "p-mate", "Mate")
    from squadmind.web.app import COOKIE
    from itsdangerous import URLSafeTimedSerializer
    mate = skip_email(TestClient(client.app))
    mate.cookies.set(COOKIE, URLSafeTimedSerializer("test-secret", salt="squadmind-oturum").dumps("s-mate"))
    assert "Kabul et" in mate.get(team_url).text
    mate.post(f"/takimlar/{team_id}/kabul")
    assert all(m["status"] == "üye" for m in store.team_members(team_id))
    assert "Birlikte oynama" in client.get(team_url).text

    # Üye başkasını çıkaramaz, kendini çıkarabilir; yabancı takımı göremez
    assert mate.post(f"/takimlar/{team_id}/cikar", data={"faceit_id": "p-self"}).status_code == 403
    mate.post(f"/takimlar/{team_id}/cikar", data={"faceit_id": "p-mate"})
    assert mate.get(team_url).status_code == 404


def test_team_add_unknown_nickname_via_faceit(oauth_web):
    client, store, _ = oauth_web
    _faceit_login(client)
    team_id = client.post("/takimlar", data={"name": "X"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    r = client.post(f"/takimlar/{team_id}/ekle", data={"nickname": "yok_boyle"}, follow_redirects=False)
    assert "yok=1" in r.headers["location"]
    assert len(store.team_members(team_id)) == 1


def test_upload_map_hint_and_map_cards(oauth_web):
    client, store, _ = oauth_web
    _faceit_login(client)
    page = client.get("/ben").text
    assert "Dust2" in page and "Mirage" in page and "en az 10 demo" in page
    r = client.post("/api/yukleme", json={"name": "a.dem", "size": 100, "map": "de_hayali"})
    assert r.status_code == 400
    r = client.post("/api/yukleme", json={"name": "a.dem", "size": 100, "map": "de_mirage"})
    assert r.status_code == 200
    assert store.upload(r.json()["id"])["map_hint"] == "de_mirage"


def test_demo_of_other_map_is_rejected_and_not_registered(web):
    # Fixture demosu Mirage: Dust2 alanına yüklenemez, sonra doğru alana yüklenebilir
    client, store = web
    _join(client)
    uid, done = _upload(client, f"{M1}-1-1.dem", b"demo-bytes", map_name="de_dust2")
    up = store.upload(uid)
    assert up["status"] == "reddedildi" and "Dust2 alanına yüklenemez" in up["message"]
    uid, done = _upload(client, f"{M1}-1-1.dem", b"demo-bytes", map_name="de_mirage")
    assert store.upload(uid)["status"] == "tamam"


def test_upload_requires_map(web):
    client, _ = web
    _join(client)
    r = client.post("/api/yukleme", json={"name": "a.dem", "size": 10})
    assert r.status_code == 400 and "haritayı seç" in r.json()["detail"]


def test_map_art_is_served_and_cached(web):
    from tests.test_board import _nav_available
    client, _ = web
    assert client.get("/harita/de_hayali.svg").status_code == 404
    if _nav_available():
        r = client.get("/harita/de_dust2.svg")
        assert r.status_code == 200 and r.text.startswith("<svg") and "max-age" in r.headers["cache-control"]


def test_faceit_authorize_url_pkce_toggle():
    from squadmind.web.faceit_auth import FaceitOAuth
    off = FaceitOAuth("id", "sec", "https://app.squadmind.info", pkce=False, flow="code").authorize_url("st", "ch")
    assert "code_challenge" not in off and "redirect_popup=true" in off and "state=st" in off
    on = FaceitOAuth("id", "sec", "https://app.squadmind.info", flow="code").authorize_url("st", "ch")
    assert on.startswith("https://accounts.faceit.com?")
    assert "code_challenge=ch" in on and "code_challenge_method=plain" in on


def test_faceit_default_scope_has_no_email_and_flow_is_code():
    # 24.10.2026: "email" istemciye tanımlı değil → FACEIT Connect 400 "Invalid scope: email",
    # "Continue as …" sessizce takılıyordu. Varsayılan: code akışı, PKCE, scope openid profile
    from urllib.parse import parse_qs, urlsplit
    from squadmind.web.faceit_auth import FaceitOAuth
    q = parse_qs(urlsplit(FaceitOAuth("id", "sec", "https://app.squadmind.info").authorize_url("st", "ch")).query)
    assert q["scope"] == ["openid profile"] and q["response_type"] == ["code"]
    assert q["code_challenge"] == ["ch"] and q["code_challenge_method"] == ["plain"]
    assert q["redirect_uri"] == ["https://app.squadmind.info/giris/faceit/donus"]


def test_faceit_exchange_basic_pkce_then_single_fallback():
    # PKCE açık (OAuth2-Template biçimi): Basic + code + grant_type + code_verifier, önce
    # redirect_uri'siz, 400/401'de redirect_uri'li; en fazla 2 istek, başka hatada bırakır
    import httpx
    from urllib.parse import parse_qs
    from squadmind.web.faceit_auth import FaceitOAuth
    for statuses, with_ru in (([200], [False]), ([401, 200], [False, True]),
                              ([401, 401], [False, True]), ([500], [False])):
        seen = []

        def handler(req: httpx.Request, statuses=statuses, seen=seen) -> httpx.Response:
            assert req.headers["authorization"].startswith("Basic ")
            body = parse_qs(req.content.decode())
            assert body["grant_type"] == ["authorization_code"] and body["code"] == ["abc123"]
            assert body["code_verifier"] == ["ver"] and "client_secret" not in body
            seen.append("redirect_uri" in body)
            return httpx.Response(statuses[len(seen) - 1], json={})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            tok = FaceitOAuth("id", "sec", "https://app.squadmind.info")._exchange(http, "abc123", "ver")
        assert seen == with_ru and tok.status_code == statuses[-1]
    # PKCE kapalıyken tek istek, doğrulayıcısız
    seen2 = []
    def h2(req: httpx.Request) -> httpx.Response:
        seen2.append(b"code_verifier" in req.content)
        return httpx.Response(401)
    with httpx.Client(transport=httpx.MockTransport(h2)) as http:
        FaceitOAuth("id", "sec", "https://app.squadmind.info", pkce=False)._exchange(http, "abc123", "ver")
    assert seen2 == [False]


def test_faceit_check_credentials_separates_bad_secret_from_bad_code():
    # Sahte kodlu kontrol: 400 = kimlik geçerli, 401 = secret reddedildi
    import httpx
    from squadmind.web.faceit_auth import FaceitOAuth
    oauth = FaceitOAuth("id", "sec", "https://app.squadmind.info", flow="code")
    for status, word in ((400, "kimlik geçerli"), (401, "secret reddedildi")):
        def handler(req: httpx.Request, status=status) -> httpx.Response:
            assert req.headers["authorization"].startswith("Basic ")
            assert b"code_verifier=" not in req.content  # doğrulayıcı secret kontrolünü atlatır
            return httpx.Response(status)
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            assert word in oauth.check_credentials(http)


def test_faceit_implicit_flow(tmp_path, make_client, monkeypatch):
    """Implicit: dönüş sayfası anahtarı POST eder, sunucu userinfo'dan kimliği alır."""
    from squadmind.web.faceit_auth import FaceitAuthError, FaceitOAuth

    oauth = FaceitOAuth("id", "sec", "https://app.squadmind.info", flow="implicit")
    url = oauth.authorize_url("st", "ch")
    assert "response_type=token" in url and "code_challenge" in url

    def fake_user(self, token):
        if token != "iyi":
            raise FaceitAuthError("geçersiz anahtar")
        return {"guid": "p-self"}

    monkeypatch.setattr(FaceitOAuth, "user_from_token", fake_user)
    store = Store(tmp_path / "web.duckdb")
    worker = Worker(store, tmp_path / "p", tmp_path / "t", client_factory=make_client)
    worker.submit = lambda job: None
    settings = WebSettings(password_login=True, invite_code=INVITE, secret="s", required_demos=10, team_owner=None,
                           map_name=None, uploads_dir=tmp_path / "u", oauth=oauth)
    client = skip_email(TestClient(create_app(settings, store, worker, client_factory=make_client)))
    try:
        r = client.get("/giris/faceit", follow_redirects=False)
        state = r.headers["location"].split("state=")[1].split("&")[0]
        back = client.get("/giris/faceit/donus")            # # kısmı sunucuya gelmez
        assert "giris/faceit/anahtar" in back.text
        bad = client.post("/giris/faceit/anahtar", data={"access_token": "kotu", "state": state})
        assert "geçersiz anahtar" in bad.text and store.users() == []
        wrong = client.post("/giris/faceit/anahtar", data={"access_token": "iyi", "state": "sahte"})
        assert "doğrulanamadı" in wrong.text
        ok = client.post("/giris/faceit/anahtar", data={"access_token": "iyi", "state": state},
                         follow_redirects=False)
        assert ok.headers["location"] == "/ben" and store.users()[0].nickname == "TestNick"
    finally:
        store.close()


def test_login_link_signs_in_known_user_only(web):
    from squadmind.web.app import make_login_link
    client, store = web
    assert "süresi dolmuş" in client.get("/giris/link?t=sahte").text
    unknown = make_login_link("test-secret", "http://x", "yok").split("?t=")[1]
    assert "kayıtlı değil" in client.get(f"/giris/link?t={unknown}").text
    _join(client)
    client.cookies.clear()
    token = make_login_link("test-secret", "http://x", SELF).split("?t=")[1]
    r = client.get(f"/giris/link?t={token}", follow_redirects=False)
    assert r.headers["location"] == "/ben" and "squadmind_oturum" in r.headers["set-cookie"]
    other = make_login_link("baska-secret", "http://x", SELF).split("?t=")[1]
    assert "süresi dolmuş" in client.get(f"/giris/link?t={other}").text


def test_arena_requires_permission_and_position(web):
    client, store = web
    _join(client)
    # İzinsiz oyuncu verisi kullanılamaz
    r = client.post("/api/arena/duel", json={"a": {"steam_id": "76561100000000099", "x": 0, "y": 0},
                                            "b": {"elo": 1500, "x": 10, "y": 10}})
    assert r.status_code == 403
    # Konum eksikse 400
    r = client.post("/api/arena/duel", json={"a": {"steam_id": SELF}, "b": {"elo": 1500}})
    assert r.status_code == 400


# --- Arena taktik modu: tek örnek akış (profesör kararı: olasılık / skor / kazanan yok) -------

_A_LONG = [[350, -61], [815, 758], [1560, 1380], [1097, 2760]]


def _flow_body(**over):
    holds = [[1097, 2760], [1560, 1380], [-515, 2050], [-1102, 2200], [-1750, 2400]]
    body = {"seed": 7,
            "t": [{"level": 9, "weapon": "rifle", "points": _A_LONG, "delay": 0.35 * i, "plant": i == 4}
                  for i in range(5)],
            "ct": [{"level": 9, "weapon": "rifle", "point": h} for h in holds]}
    body.update(over)
    return body


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def _needs_dust2():
    from awpy import data
    if not data.mesh_path("de_dust2").exists():
        pytest.skip("Dust2 geometrisi yerelde yok")


def test_taktik_flow_requires_login_and_permission(web):
    client, _ = web
    assert client.post("/api/arena/taktik/akis", json=_flow_body()).status_code == 401
    _join(client)
    # İzinsiz oyuncu verisi (T ya da CT tarafında) reddedilir; hiçbir hesap yapılmadan
    bad_t = _flow_body()
    bad_t["t"][0] = {"steam_id": "76561100000000099", "weapon": "rifle", "points": []}
    assert client.post("/api/arena/taktik/akis", json=bad_t).status_code == 403
    bad_ct = _flow_body()
    bad_ct["ct"][2] = {"steam_id": "76561100000000099", "weapon": "rifle", "point": [0, 0]}
    assert client.post("/api/arena/taktik/akis", json=bad_ct).status_code == 403
    # Biçim hataları
    assert client.post("/api/arena/taktik/akis", json=_flow_body(t=[])).status_code == 400
    assert client.post("/api/arena/taktik/akis", json=_flow_body(seed=-1)).status_code == 400
    wrong_weapon = _flow_body()
    wrong_weapon["ct"][0]["weapon"] = "bazuka"
    assert client.post("/api/arena/taktik/akis", json=wrong_weapon).status_code == 400
    far = _flow_body()
    far["t"][0]["points"] = [[1e9, 0]]
    assert client.post("/api/arena/taktik/akis", json=far).status_code == 400


def test_taktik_flow_has_no_probability_fields_and_repeats_with_seed(web):
    _needs_dust2()
    client, _ = web
    _join(client)
    r1 = client.post("/api/arena/taktik/akis", json=_flow_body())
    assert r1.status_code == 200, r1.text
    flow = r1.json()
    ks = set(_keys(flow))
    assert not ks & {"p", "win", "prob", "probability", "lo", "hi", "p_t", "score", "winner"}
    assert not any("prob" in k.lower() or "win" in k.lower() for k in ks)
    assert flow["label"] == "Örnek akış: tahmin değil, betimleme. Sınamayı geçmedi."
    assert len(flow["units"]) == 10 and flow["events"] is not None
    # Aynı tohum aynı akış; başka tohumlardan en az biri farklı
    assert client.post("/api/arena/taktik/akis", json=_flow_body()).json() == flow
    others = [client.post("/api/arena/taktik/akis", json=_flow_body(seed=s)).json() for s in range(8, 20)]
    assert any(o != flow for o in others)


def test_taktik_route_preview_snaps_and_validates(web):
    _needs_dust2()
    client, _ = web
    assert client.post("/api/arena/taktik/rota", json={"slot": 0, "points": _A_LONG}).status_code == 401
    _join(client)
    r = client.post("/api/arena/taktik/rota", json={"slot": 0, "points": _A_LONG})
    assert r.status_code == 200 and len(r.json()["points"]) == 4 and len(r.json()["path"]) >= 4
    assert client.post("/api/arena/taktik/rota", json={"slot": 9, "points": _A_LONG}).status_code == 400
    assert client.post("/api/arena/taktik/rota", json={"slot": 0, "points": [[0, 0]] * 40}).status_code == 400
    ct = client.post("/api/arena/taktik/rota", json={"side": "ct", "points": [[1097, 2760]]})
    assert ct.status_code == 200 and len(ct.json()["points"]) == 1


def test_taktik_page_renders_with_fixed_label(web):
    _needs_dust2()
    client, _ = web
    assert client.get("/arena/taktik", follow_redirects=False).status_code == 303
    _join(client)
    page = client.get("/arena/taktik")
    assert page.status_code == 200
    assert "Örnek akış: tahmin değil, betimleme. Sınamayı geçmedi." in page.text
