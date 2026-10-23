"""FACEIT'ten getir (Downloads API) ve DEMO_READY webhook testleri.

Gerçek FACEIT'e istek ATILMAZ: Data / Downloads API httpx MockTransport ile, indirme ve demo
ayrıştırıcı sahte. Tüm özellikler `faceit_downloads` bayrağı olmadan kapalıdır.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from squadmind.db import connect, init_db
from squadmind.web.app import FACEIT_RATE_PER_MIN, WebSettings, create_app
from squadmind.web.store import Store
from squadmind.web.worker import Worker
from tests.conftest import fake_faceit_handler, skip_email
from tests.test_ingest_demos import M1, SELF, make_tables

INVITE = "takim-davet-123"
SECRET = "webhook-gizli"
HEADER = "X-Squadmind-Webhook"
SIGNED = "https://signed.example/demo.dem.zst?sig=abc"
OTHER = "1-00000000-0000-0000-0000-000000000000"  # kullanıcının oynamadığı maç


def downloads_handler(request: httpx.Request) -> httpx.Response:
    """Data API fixture'ları + Downloads API (imzalı link)."""
    if request.url.path == "/download/v2/demos/download":
        return httpx.Response(200, json={"payload": {"download_url": SIGNED}})
    return fake_faceit_handler(request)


def build(tmp_path, make_client, *, enabled: bool, secret: str = SECRET, handler=downloads_handler):
    store = Store(tmp_path / "web.duckdb")
    downloaded: list[str] = []

    def fake_download(url, dest):
        downloaded.append(url)
        dest.write_bytes(b"demo-bytes")
        return 10

    worker = Worker(store, tmp_path / "processed", tmp_path / "tmp",
                    client_factory=lambda: make_client(handler),
                    parser=lambda p: make_tables(), downloader=fake_download)

    def run_now(job):
        worker.handle(job)
        if job.kind == "sync":  # fixture maç ID'si "m-1": gerçek FACEIT biçimine çevir
            store.run(lambda con: [con.execute(
                f"UPDATE {t} SET match_id = ? WHERE match_id = 'm-1'", [M1])
                for t in ("matches", "match_player_stats")])

    worker.submit = run_now
    settings = WebSettings(invite_code=INVITE, secret="test-secret", required_demos=10,
                           team_owner=None, map_name=None, uploads_dir=tmp_path / "uploads",
                           faceit_downloads=enabled, webhook_secret=secret, webhook_header=HEADER)
    app = create_app(settings, store, worker, client_factory=lambda: make_client(handler))
    app.state.worker = worker
    return skip_email(TestClient(app)), store, downloaded


@pytest.fixture
def dl(tmp_path, make_client):
    client, store, downloaded = build(tmp_path, make_client, enabled=True)
    yield client, store, downloaded
    store.close()


@pytest.fixture
def off(tmp_path, make_client):
    client, store, _ = build(tmp_path, make_client, enabled=False)
    yield client, store
    store.close()


def join(client):
    r = client.post("/katil", data={"nickname": "TestNick", "davet": INVITE}, follow_redirects=False)
    assert r.status_code == 303


def event(match_id: str = M1, name: str = "match_demo_ready", tx: str = "t-1") -> dict:
    return {"transaction_id": tx, "event": name, "payload": {"id": match_id, "demo_url": [SIGNED]}}


def hook(client, body, secret: str | None = SECRET, **kw):
    headers = {HEADER: secret} if secret is not None else {}
    return client.post("/api/faceit/webhook", json=body, headers=headers, **kw)


# --- bayrak kapalı --------------------------------------------------------------------

def test_everything_is_404_when_flag_off(off):
    client, _ = off
    join(client)
    assert client.get("/api/faceit/maclar").status_code == 404
    assert client.post("/api/faceit/demo-link", json={"match_id": M1}).status_code == 404
    assert client.post("/api/faceit/sunucu-isle", json={"match_id": M1}).status_code == 404
    assert client.post("/ben/otomatik-demo", data={"acik": "1"}).status_code == 404
    # Arayüzde görünmez
    assert "FACEIT maçlarım" not in client.get("/demolar").text
    assert "Otomatik demo" not in client.get("/ben").text


def test_webhook_404_without_secret_and_records_only_when_flag_off(tmp_path, make_client):
    client, store, _ = build(tmp_path, make_client, enabled=False, secret="")
    assert hook(client, event()).status_code == 404
    store.close()


def test_flag_off_webhook_records_but_does_not_process(off):
    client, store = off
    join(client)
    store.set_auto_demo(SELF, True)
    r = hook(client, event())
    assert r.status_code == 200 and r.json()["recorded"] is True
    assert store.demo_event(M1)["status"] == "kaydedildi"
    assert store.done_count(SELF) == 0


# --- liste ve link ---------------------------------------------------------------------

def test_endpoints_require_session(dl):
    client, _, _ = dl
    assert client.get("/api/faceit/maclar").status_code == 401
    assert client.post("/api/faceit/demo-link", json={"match_id": M1}).status_code == 401
    assert client.post("/api/faceit/sunucu-isle", json={"match_id": M1}).status_code == 401


def test_match_list_and_ui_visible(dl):
    client, _, _ = dl
    join(client)
    ms = client.get("/api/faceit/maclar").json()["matches"]
    m = next(x for x in ms if x["match_id"] == M1)
    assert m["has_demo"] and not m["processed"] and m["supported"]
    assert "FACEIT maçlarım" in client.get("/demolar").text
    assert "Otomatik demo" in client.get("/ben").text


def test_demo_link_returns_signed_url_uncached(dl):
    client, _, _ = dl
    join(client)
    r = client.post("/api/faceit/demo-link", json={"match_id": M1})
    assert r.status_code == 200
    assert r.json()["url"] == SIGNED and r.json()["name"].startswith(M1)
    assert r.headers["cache-control"] == "no-store"


def test_demo_link_rejects_foreign_match_and_bad_id(dl):
    client, _, _ = dl
    join(client)
    assert client.post("/api/faceit/demo-link", json={"match_id": OTHER}).status_code == 403
    assert client.post("/api/faceit/demo-link", json={"match_id": "../x"}).status_code == 400
    assert client.post("/api/faceit/demo-link", content=b"yok").status_code == 400


def test_processed_match_is_refused(dl):
    client, store, _ = dl
    join(client)
    store.run(lambda con: con.execute("UPDATE matches SET demo_status = 'processed'"))
    assert client.post("/api/faceit/demo-link", json={"match_id": M1}).status_code == 409
    assert client.post("/api/faceit/sunucu-isle", json={"match_id": M1}).status_code == 409


def test_demo_link_rate_limit(dl):
    client, _, _ = dl
    join(client)
    codes = [client.post("/api/faceit/demo-link", json={"match_id": M1}).status_code
             for _ in range(FACEIT_RATE_PER_MIN + 1)]
    assert codes[:FACEIT_RATE_PER_MIN] == [200] * FACEIT_RATE_PER_MIN and codes[-1] == 429


def test_demo_link_without_downloads_permission_is_502(tmp_path, make_client):
    def forbidden(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/download/v2/demos/download":
            return httpx.Response(403, json={"message": "no valid scope"})
        return fake_faceit_handler(request)

    client, store, _ = build(tmp_path, make_client, enabled=True, handler=forbidden)
    join(client)
    assert client.post("/api/faceit/demo-link", json={"match_id": M1}).status_code == 502
    store.close()


# --- sunucu yolu: indir, işle, SİL ----------------------------------------------------

def test_server_path_processes_and_deletes_file(dl, tmp_path):
    client, store, downloaded = dl
    join(client)
    r = client.post("/api/faceit/sunucu-isle", json={"match_id": M1})
    assert r.status_code == 200, r.text
    up = store.upload(r.json()["id"])
    assert (up["status"], up["match_id"]) == ("tamam", M1)
    assert downloaded == [SIGNED]
    row = store.run(lambda con: con.execute(
        "SELECT source, uploaded_by FROM demo_files WHERE match_id = ?", [M1]).fetchone())
    assert row == ("faceit-sunucu", SELF)
    # Taahhüt: demo dosyası hiçbir yerde kalmaz (ne tmp'de ne processed/'ta)
    left = [p for d in ("tmp", "processed", "uploads") if (tmp_path / d).exists()
            for p in (tmp_path / d).rglob("*") if p.is_file()]
    assert left == []
    # Aynı maç ikinci kez istenemez
    assert client.post("/api/faceit/sunucu-isle", json={"match_id": M1}).status_code == 409


def test_server_path_deletes_file_even_when_mismatch(tmp_path, make_client):
    client, store, _ = build(tmp_path, make_client, enabled=True)
    join(client)
    # FACEIT skor tablosuyla tutmayan demo: dosya processed/'a değil, hiçbir yere taşınmaz
    store.run(lambda con: con.execute("UPDATE match_player_stats SET kills = kills + 50"))
    r = client.post("/api/faceit/sunucu-isle", json={"match_id": M1})
    up = store.upload(r.json()["id"])
    assert up["status"] == "tutmadı"
    assert [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".dem", ".zst", ".gz")] == []
    store.close()


# --- tarayıcı yolu kaydı --------------------------------------------------------------

def test_browser_path_source_label(tmp_path, make_client):
    import gzip

    client, store, _ = build(tmp_path, make_client, enabled=True)
    client.app.state.worker.package_parser = lambda b: make_tables()
    join(client)
    store.run(lambda con: con.execute("UPDATE matches SET score_a = 1, score_b = 1"))
    q = {"name": f"{M1}.dem.zst", "sha256": "c" * 64, "dem_sha256": "d" * 64, "size": 99,
         "map": "de_mirage", "kaynak": "faceit"}
    r = client.post("/api/paket", params=q, content=gzip.compress(b"{}"))
    assert r.status_code == 200, r.text
    assert store.upload(r.json()["id"])["status"] == "tamam"
    assert store.run(lambda con: con.execute(
        "SELECT source FROM demo_files WHERE sha256 = ?", ["c" * 64]).fetchone()[0]) == "faceit-tarayici"
    store.close()


# --- webhook --------------------------------------------------------------------------

@pytest.mark.parametrize("secret", [None, "yanlis", ""])
def test_webhook_rejects_missing_or_wrong_secret(dl, secret):
    client, store, _ = dl
    assert hook(client, event(), secret=secret).status_code == 401
    assert store.demo_event(M1) is None


def test_webhook_query_secret_also_accepted(dl):
    client, store, _ = dl
    r = client.post("/api/faceit/webhook", params={"gizli": SECRET}, json=event())
    assert r.status_code == 200 and store.demo_event(M1)["resource_url"] == SIGNED


def test_webhook_records_event_once(dl):
    client, store, _ = dl
    assert hook(client, event()).json() == {"ok": True, "recorded": True}
    again = hook(client, event(tx="t-2"))
    assert again.status_code == 200 and again.json()["duplicate"] is True
    ev = store.demo_event(M1)
    assert ev["transaction_id"] == "t-1" and ev["status"] == "atlandı"  # kullanıcı yok
    assert store.run(lambda con: con.execute(
        "SELECT count(*) FROM faceit_demo_events").fetchone()[0]) == 1


def test_webhook_other_events_and_bad_payloads(dl):
    client, store, _ = dl
    assert hook(client, event(name="match_status_finished")).json()["ignored"] == "match_status_finished"
    assert store.demo_event(M1) is None
    assert hook(client, event(match_id="x")).status_code == 400
    assert hook(client, [1, 2]).status_code == 400


def test_webhook_queues_for_user_with_auto_demo(dl, tmp_path):
    client, store, downloaded = dl
    join(client)
    r = hook(client, event())
    assert r.json()["recorded"] is True and store.done_count(SELF) == 0  # izin kapalı: kuyruk yok

    store.run(lambda con: con.execute("DELETE FROM faceit_demo_events"))
    client.post("/ben/otomatik-demo", data={"acik": "1"})
    assert store.user(SELF).auto_demo is True
    r = hook(client, event())
    assert r.json()["queued"] is True
    assert downloaded == [SIGNED]
    ev = store.demo_event(M1)
    assert (ev["status"], ev["processed_for"]) == ("tamam", SELF)
    assert store.done_count(SELF) == 1
    assert [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".dem", ".zst", ".gz")] == []


def test_schema_v10_migration_is_idempotent():
    con = connect(":memory:")
    init_db(con)
    con.execute("DELETE FROM schema_version")
    con.execute("INSERT INTO schema_version VALUES (9)")
    init_db(con)
    assert con.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 11
    cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'web_users'").fetchall()}
    assert "auto_demo" in cols
    con.close()
