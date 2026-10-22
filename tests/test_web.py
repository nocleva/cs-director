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
