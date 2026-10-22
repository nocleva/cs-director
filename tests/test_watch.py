"""`director watch` testleri: indirme bekleme, olay filtresi, indirilen demonun işlenmesi."""

from __future__ import annotations

import os
import queue
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileMovedEvent

from director.db import connect, init_db
from director.ingest.watch import (
    _DemoEventHandler,
    is_demo_file,
    process_download,
    wait_until_stable,
)
from tests.test_ingest_demos import M1, _sync, make_tables


class FakeClock:
    """Gerçek beklemeden zamanı ilerleten saat; her uykuda isteğe bağlı bir iş yapar."""

    def __init__(self, on_sleep=None) -> None:
        self.t = 0.0
        self.calls = 0
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.calls += 1
        if self.on_sleep:
            self.on_sleep(self.calls)
        self.t += s


def test_is_demo_file():
    assert is_demo_file(Path(f"{M1}-1-1.dem.zst"))
    assert is_demo_file(Path("mac.DEM.GZ"))
    assert is_demo_file(Path("mac.dem"))
    assert not is_demo_file(Path(f"{M1}-1-1.dem.zst.crdownload"))
    assert not is_demo_file(Path("notlar.txt"))


def test_wait_until_stable_waits_for_growth_to_stop(tmp_path):
    f = tmp_path / "a.dem.zst"
    f.write_bytes(b"x")

    # İlk 4 uykuda dosya büyüyor (indirme sürüyor), sonra duruyor
    def grow(n):
        if n <= 4:
            with f.open("ab") as fh:
                fh.write(b"x" * 10)

    clock = FakeClock(grow)
    assert wait_until_stable(f, quiet=3.0, poll=0.5, clock=clock, sleep=clock.sleep)
    # Son büyüme 4. uykuda (t=2.0'a kadar); en az 3 sn sessizlik → t ≥ 5.0
    assert clock.t >= 5.0
    assert f.stat().st_size == 41


def test_wait_until_stable_false_when_file_vanishes_or_timeout(tmp_path):
    f = tmp_path / "a.dem.zst"
    f.write_bytes(b"x")
    clock = FakeClock(lambda n: f.unlink() if n == 1 else None)
    assert not wait_until_stable(f, clock=clock, sleep=clock.sleep)

    # Boş dosya hiç dolmazsa bitmiş sayılmaz → zaman aşımı
    empty = tmp_path / "b.dem"
    empty.write_bytes(b"")
    clock = FakeClock()
    assert not wait_until_stable(empty, timeout=10.0, clock=clock, sleep=clock.sleep)


def test_event_handler_queues_finished_demos_only():
    q: queue.Queue[Path] = queue.Queue()
    h = _DemoEventHandler(q)
    h.on_created(FileCreatedEvent("C:/dl/x.dem.zst.crdownload"))  # indirme sürüyor
    h.on_moved(FileMovedEvent("C:/dl/x.dem.zst.crdownload", "C:/dl/x.dem.zst"))  # bitti
    h.on_created(FileCreatedEvent("C:/dl/resim.png"))
    assert [q.get_nowait().name for _ in range(q.qsize())] == ["x.dem.zst"]


def _db(tmp_path, make_client) -> Path:
    db = tmp_path / "t.duckdb"
    with connect(db) as con:
        init_db(con)
        _sync(make_client, con)
    return db


def test_process_download_moves_to_inbox_and_ingests(tmp_path, make_client):
    db = _db(tmp_path, make_client)
    downloads, inbox = tmp_path / "Downloads", tmp_path / "inbox"
    downloads.mkdir()
    demo = downloads / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")

    r = process_download(demo, lambda: connect(db), inbox, tmp_path / "processed",
                         tmp_path / "tmp", parser=lambda p: make_tables(), wait=lambda p: True)

    assert r is not None and r.error is None and r.match_id == M1
    assert not demo.exists()  # indirme klasöründen alındı
    assert (tmp_path / "processed" / demo.name).exists()
    with connect(db, read_only=True) as con:
        assert con.execute("SELECT demo_status FROM matches WHERE match_id = ?",
                           [M1]).fetchone()[0] == "processed"


def test_process_download_skips_unfinished_or_non_demo(tmp_path):
    f = tmp_path / "x.dem.zst"
    f.write_bytes(b"x")

    def never_connect():
        raise AssertionError("DB'ye bağlanmamalı")

    assert process_download(f, never_connect, tmp_path / "inbox", tmp_path / "p",
                            tmp_path / "t", wait=lambda p: False) is None
    assert f.exists()  # yerinde kaldı
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    assert process_download(txt, never_connect, tmp_path / "inbox", tmp_path / "p",
                            tmp_path / "t") is None


def test_roster_match_rejects_matches_after_download_time(tmp_path, make_client):
    """Dosya adında ID yoksa: demo, dosya tarihinden sonra başlayan maça eşlenmez."""
    import director.ingest.demos as demos_mod

    db = _db(tmp_path, make_client)
    old = demos_mod.MIN_ROSTER_OVERLAP
    demos_mod.MIN_ROSTER_OVERLAP = 4  # fixture'da kadro 4 kişi
    try:
        for when, expected in ((946684800, None), (None, M1)):  # 2000-01-01 / şimdi
            downloads = tmp_path / f"dl{when}"
            downloads.mkdir()
            demo = downloads / "isimsiz.dem"
            demo.write_bytes(b"demo")
            if when:
                os.utime(demo, (when, when))
            r = process_download(demo, lambda: connect(db), tmp_path / f"inbox{when}",
                                 tmp_path / "processed", tmp_path / "tmp",
                                 parser=lambda p: make_tables(), wait=lambda p: True)
            assert r.match_id == expected
    finally:
        demos_mod.MIN_ROSTER_OVERLAP = old
