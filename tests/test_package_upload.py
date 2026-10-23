"""Tarayıcı paketi yolu (`/api/paket`, `ingest_package`, `precheck_package`) testleri.

Gerçek demo gerektirmez: paket ayrıştırıcısı sahte tablolarla değiştirilir (test_ingest_demos).
Gerçek demoyla uçtan uca doğrulama `scripts/` altında ve Node ile ayrıca koşulur.
"""

from __future__ import annotations

import gzip
import json

import polars as pl
import pytest

from squadmind.db import connect, init_db
from squadmind.ingest.demos import ingest_package, precheck_package
from squadmind.parse.package import PackageError, check_compatibility
from squadmind.web.app import PACKAGE_MAX_BYTES
from tests.test_ingest_demos import M1, SELF, _sync, make_tables
from tests.test_web import _join, web  # noqa: F401  (web: pytest fixture)

SHA = "a" * 64
DEM_SHA = "b" * 64
PACK = gzip.compress(b"{}")  # içeriği sahte ayrıştırıcı yok sayar


def _post(client, data: bytes = PACK, name: str | None = None, sha: str = SHA,
          dem_sha: str = DEM_SHA, map_name: str = "de_mirage"):
    q = {"name": name or f"{M1}-1-1.dem.zst", "sha256": sha, "dem_sha256": dem_sha,
         "size": 123456789, "map": map_name}
    return client.post("/api/paket", params=q, content=data)


@pytest.fixture
def pkg_web(web):  # noqa: F811
    client, store = web
    client.app.state.worker.package_parser = lambda b: make_tables()
    # Fixture maçı 2 round'luk sahte demoya uysun
    store.run(lambda con: con.execute("UPDATE matches SET score_a = 1, score_b = 1"))
    return client, store


def _joined(pkg_web):
    client, store = pkg_web
    _join(client)
    # sync sonrası maç ID'si M1'e çevrilir (run_now); skor güncellemesi sync'ten sonra olmalı
    store.run(lambda con: con.execute("UPDATE matches SET score_a = 1, score_b = 1"))
    return client, store


def test_package_requires_session(pkg_web):
    client, _ = pkg_web
    assert _post(client).status_code == 401


def test_package_valid_is_processed_and_recorded_as_package(pkg_web, tmp_path):
    client, store = _joined(pkg_web)
    r = _post(client)
    assert r.status_code == 200, r.text
    up = store.upload(r.json()["id"])
    assert (up["status"], up["match_id"]) == ("tamam", M1)
    row = store.run(lambda con: con.execute(
        "SELECT source, dem_sha256, status, uploaded_by FROM demo_files WHERE sha256 = ?",
        [SHA]).fetchone())
    assert row == ("paket", DEM_SHA, "processed", SELF)
    # Sunucuda paket dosyası kalmaz
    assert not list((tmp_path / "uploads").rglob("paket.json.gz"))
    assert store.run(lambda con: con.execute(
        "SELECT demo_status FROM matches WHERE match_id = ?", [M1]).fetchone()[0]) == "processed"


def test_package_duplicate_is_rejected(pkg_web):
    client, _ = _joined(pkg_web)
    assert _post(client).status_code == 200
    # Aynı özet, aynı açılmış .dem özeti (farklı dosya adı/özet), aynı maç: hepsi 409
    assert _post(client, name="baska.dem").status_code == 409
    assert _post(client, name="baska.dem", sha="c" * 64).status_code == 409  # dem_sha256 aynı
    r2 = _post(client, sha="f" * 64, dem_sha="9" * 64)    # dosya adındaki maç zaten işlenmiş
    assert r2.status_code == 409 and "zaten işlendi" in r2.json()["detail"]


def test_package_cross_format_duplicate_via_full_demo_sha(pkg_web):
    """Tam demo yoluyla eklenen dosyanın özeti, paket yolunda sha256 olarak yakalanır."""
    client, store = _joined(pkg_web)
    store.run(lambda con: con.execute(
        "INSERT INTO demo_files (sha256, match_id, file_name, status) VALUES (?, 'x', 'a.dem', 'processed')",
        [SHA]))
    assert _post(client, name="rastgele.dem").status_code == 409


def test_package_size_limit_and_format(pkg_web):
    client, _ = _joined(pkg_web)
    big = b"\x1f\x8b" + b"0" * PACKAGE_MAX_BYTES
    assert _post(client, data=big).status_code == 413
    assert _post(client, data=b"gzip degil").status_code == 400
    assert _post(client, name="virus.exe").status_code == 400
    assert _post(client, map_name="de_yok").status_code == 400
    assert _post(client, sha="xyz").status_code == 409  # geçersiz özet


def test_package_wrong_map_is_rejected_and_not_registered(pkg_web):
    client, store = _joined(pkg_web)
    r = _post(client, map_name="de_dust2")
    up = store.upload(r.json()["id"])
    assert up["status"] == "reddedildi" and "haritasına" in up["message"]
    assert store.run(lambda con: con.execute("SELECT count(*) FROM demo_files").fetchone()[0]) == 0


def test_package_version_mismatch_is_rejected(pkg_web):
    client, store = _joined(pkg_web)
    client.app.state.worker.package_parser = None  # gerçek (sürüm denetleyen) ayrıştırıcı
    bad = gzip.compress(json.dumps({"format_version": 1, "awpy_version": "0.0.1"}).encode())
    up = store.upload(_post(client, data=bad).json()["id"])
    assert up["status"] == "reddedildi"
    assert "sürümü uyuşmuyor" in up["message"]
    wrong_format = gzip.compress(json.dumps({"format_version": 99}).encode())
    up = store.upload(_post(client, data=wrong_format, sha="1" * 64, dem_sha="2" * 64).json()["id"])
    assert up["status"] == "reddedildi" and "biçimi" in up["message"]


def test_check_compatibility_rules():
    check_compatibility({"format_version": 1, "awpy_version": "3.1.1"}, "3.1.1")
    with pytest.raises(PackageError, match="ayrıştırıcı"):
        check_compatibility({"format_version": 1, "awpy_version": "3.0.0"}, "3.1.1")
    with pytest.raises(PackageError, match="biçimi"):
        check_compatibility({"format_version": 2, "awpy_version": "3.1.1"}, "3.1.1")


def test_package_round_count_mismatch_is_not_saved(pkg_web):
    client, store = _joined(pkg_web)
    store.run(lambda con: con.execute("UPDATE matches SET score_a = 13, score_b = 9"))
    up = store.upload(_post(client).json()["id"])
    assert up["status"] == "tutmadı" and "round sayısı" in up["message"]
    assert store.run(lambda con: con.execute("SELECT count(*) FROM rounds").fetchone()[0]) == 0
    assert store.run(lambda con: con.execute(
        "SELECT demo_status FROM matches WHERE match_id = ?", [M1]).fetchone()[0]) == "mismatch"
    # Aynı sahte paket tekrar denenemez
    assert _post(client).status_code == 409


def test_package_foreign_players_are_not_saved(pkg_web):
    client, store = _joined(pkg_web)

    def foreign(_b: bytes):
        t = make_tables()
        t.players = pl.DataFrame({"steamid": [SELF, "76561198999999991", "76561198999999992"]})
        return t
    client.app.state.worker.package_parser = foreign
    up = store.upload(_post(client).json()["id"])
    assert up["status"] == "tutmadı" and "kadro" in up["message"]


def test_package_with_empty_stats_is_not_accepted(pkg_web):
    client, store = _joined(pkg_web)

    def empty_stats(_b: bytes):
        t = make_tables()
        t.player_stats = t.player_stats.head(0)
        return t
    client.app.state.worker.package_parser = empty_stats
    up = store.upload(_post(client).json()["id"])
    assert up["status"] == "tutmadı" and "eksik" in up["message"]
    assert store.run(lambda con: con.execute("SELECT count(*) FROM kills").fetchone()[0]) == 0


def test_package_stats_off_from_faceit_are_mismatch(pkg_web):
    client, store = _joined(pkg_web)

    def inflated(_b: bytes):
        t = make_tables()
        t.player_stats = t.player_stats.with_columns(pl.col("kills") + 10)
        return t
    client.app.state.worker.package_parser = inflated
    up = store.upload(_post(client).json()["id"])
    assert up["status"] == "tutmadı"
    assert store.run(lambda con: con.execute("SELECT count(*) FROM kills").fetchone()[0]) == 0


def test_precheck_package_variants(tmp_path, make_client):
    con = connect(":memory:")
    init_db(con)
    _sync(make_client, con)
    assert precheck_package(con, "x.dem", "yanlis") == "dosya özeti geçersiz"
    assert precheck_package(con, "x.dem", SHA, "kisa") == "dosya özeti geçersiz"
    assert precheck_package(con, "x.dem", SHA, DEM_SHA) is None
    res = ingest_package(con, PACK, f"{M1}-1-1.dem.zst", SHA, DEM_SHA,
                         parser=lambda b: make_tables())
    # Maç skoru (fixture) sahte demoyla uyuşmayabilir; kayıt her durumda özetleri içerir
    assert res.error is None or res.rejected is False
    assert "daha önce eklendi" in (precheck_package(con, "y.dem", SHA) or "")
    assert "daha önce eklendi" in (precheck_package(con, "y.dem", "c" * 64, DEM_SHA) or "")
