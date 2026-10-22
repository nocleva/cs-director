"""Aynı demo bir kez: boyut, içerik özeti, maç ve yükleyen kontrolleri."""

from __future__ import annotations

from pathlib import Path

import director.ingest.demos as demos
from director.ingest.demos import ingest_demo
from tests.test_ingest_demos import M1, SELF, _dirs, _sync, make_tables


def _ingest(con, demo: Path, tmp_path: Path, **kw):
    return ingest_demo(con, demo, tmp_path / "tmp", tmp_path / "processed",
                       parser=lambda p: make_tables(), **kw)


def test_size_out_of_range_is_rejected(tmp_path, con, monkeypatch):
    monkeypatch.setattr(demos, "MIN_DEMO_BYTES", 100)  # gerçek sınır yerine küçük bir eşik
    inbox, _, _ = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"x" * 10)

    r = _ingest(con, demo, tmp_path)
    assert r.rejected and "boyutu" in r.error
    assert demo.exists()  # yerinde kalır, hiç işlenmez


def test_real_upper_limit_covers_faceit_demos():
    # 18 gerçek FACEIT demosu sıkıştırılmış 114-222 MB; açılmış .dem birkaç katı
    # (alt sınır testlerde kapatıldığı için burada sadece üst sınır kontrol edilir)
    assert demos.MAX_DEMO_BYTES > 3 * 222 * 1024 * 1024


def test_same_content_renamed_is_rejected(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, _, _ = _dirs(tmp_path)
    first = inbox / f"{M1}-1-1.dem"
    first.write_bytes(b"ayni demo")
    assert _ingest(con, first, tmp_path).error is None

    # Aynı bayt, farklı ad (dosya adında maç ID'si yok)
    copy = inbox / "baska_isim.dem"
    copy.write_bytes(b"ayni demo")
    r = _ingest(con, copy, tmp_path)
    assert r.rejected and "daha önce eklendi" in r.error
    assert con.execute("SELECT count(*) FROM demo_files").fetchone()[0] == 1


def test_same_match_different_file_is_rejected(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, _, _ = _dirs(tmp_path)
    (inbox / f"{M1}-1-1.dem").write_bytes(b"ilk")
    assert _ingest(con, inbox / f"{M1}-1-1.dem", tmp_path).error is None

    # Aynı maç ID'li ama içeriği farklı dosya
    other = inbox / f"{M1}-1-1.dem"
    other.write_bytes(b"farkli icerik")
    r = _ingest(con, other, tmp_path)
    assert r.rejected and "zaten işlendi" in r.error


def test_match_found_by_roster_is_also_checked(tmp_path, make_client, con, monkeypatch):
    """Adında ID olmayan demo kadrodan zaten işlenmiş maça eşlenirse reddedilir."""
    monkeypatch.setattr(demos, "MIN_ROSTER_OVERLAP", 4)  # fixture kadrosu 4 kişi
    _sync(make_client, con)
    inbox, _, _ = _dirs(tmp_path)
    (inbox / f"{M1}-1-1.dem").write_bytes(b"ilk")
    assert _ingest(con, inbox / f"{M1}-1-1.dem", tmp_path).error is None

    anon = inbox / "isimsiz.dem"
    anon.write_bytes(b"baska bayt")
    r = _ingest(con, anon, tmp_path)
    assert r.rejected and r.match_id == M1 and "zaten işlendi" in r.error


def test_uploader_must_be_in_the_match(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, _, _ = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")

    r = _ingest(con, demo, tmp_path, uploader="76561190000000999")
    assert r.rejected and "oynamamış" in r.error
    # Maçtaki biri yükleyince kabul edilir ve yükleyen kaydedilir
    r = _ingest(con, demo, tmp_path, uploader=SELF)
    assert r.error is None
    assert con.execute("SELECT uploaded_by FROM demo_files").fetchone()[0] == SELF


def test_mismatch_demo_is_remembered(tmp_path, make_client, con):
    """FACEIT ile tutmayan demo da kaydedilir: aynı dosya tekrar denenemez."""
    _sync(make_client, con)
    inbox, _, _ = _dirs(tmp_path)
    tables = make_tables()
    tables.player_stats = tables.player_stats.with_columns(
        (tables.player_stats["kills"] + 10).alias("kills"))  # skor tablosuyla tutmaz
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"bozuk")
    r = ingest_demo(con, demo, tmp_path / "tmp", tmp_path / "processed",
                    parser=lambda p: tables)
    assert r.validated is False
    assert con.execute("SELECT status FROM demo_files").fetchone()[0] == "mismatch"

    again = inbox / "tekrar.dem"
    again.write_bytes(b"bozuk")
    assert _ingest(con, again, tmp_path).rejected


def test_reprocess_allows_known_demo(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, _, _ = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")
    assert _ingest(con, demo, tmp_path).error is None
    processed = tmp_path / "processed" / demo.name

    r = _ingest(con, processed, tmp_path, allow_known=True)
    assert r.error is None and not r.rejected
