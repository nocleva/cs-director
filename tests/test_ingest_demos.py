"""Demo ingest testleri: maç eşleştirme, DB yazma, KAST, dosya taşıma (sahte parser)."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import zstandard

from director.ingest.demos import (
    decompress,
    find_demos,
    ingest_demo,
    ingest_inbox,
    match_id_from_filename,
)
from director.ingest.matches import sync_player_matches
from director.parse.demo_parser import DemoTables

M1 = "1-9a8491dc-c098-4a8a-9193-dcc3ffc4831a"  # gerçek format; fixture'daki m-1 değil

# Fixture m-1 kadrosu: faction1 = self + mate, faction2 = enemy1 + enemy2
SELF, MATE, EN1, EN2 = (f"7656119800000000{i}" for i in range(1, 5))


def make_tables(map_name: str = "de_mirage") -> DemoTables:
    """m-1 maçına uyan küçük, yapay demo tabloları."""
    rounds = pl.DataFrame({
        "round_no": [1, 2], "winner_side": ["CT", "T"], "reason": ["ct_win", "terrorists_win"],
        "freeze_end_tick": [500, 1200], "end_tick": [900, 1800],
        "ct_buy_type": ["pistol", "eco"], "ct_equipment": [4000, 2000],
        "t_buy_type": ["pistol", "full"], "t_equipment": [3500, 20000],
        "bomb_site": [None, "A"], "plant_tick": [None, 1700],
    })
    kills = pl.DataFrame({
        "round_no": [1, 2], "tick": [600, 1300], "time_in_round": [1.5, 1.5],
        "attacker": [SELF, EN1], "attacker_name": ["TestNick", "Enemy1"],
        "attacker_side": ["CT", "T"], "victim": [EN1, MATE],
        "victim_name": ["Enemy1", "Mate"], "victim_side": ["T", "CT"],
        "weapon": ["usp_silencer", "ak47"], "attacker_place": ["LongA", "Middle"],
        "victim_place": ["LongDoors", "Middle"], "is_opening": [True, True],
        "is_traded": [False, True],
    })
    utility = pl.DataFrame({
        "round_no": [2], "tick": [1250], "thrower": [EN2], "thrower_name": ["Enemy2"],
        "thrower_side": ["T"], "type": ["smoke"], "land_place": ["CTSpawn"],
    })
    # Round 1: self + mate CT (faction1); round 2: enemy'ler CT (faction2)
    positions = pl.DataFrame({
        "round_no": [1, 1, 1, 2, 2],
        "tick": [600, 600, 600, 1300, 1300],
        "player": [SELF, MATE, EN1, EN1, EN2],
        "side": ["CT", "CT", "T", "CT", "CT"],
        "place_name": ["LongA", "BombsiteB", "LongDoors", "BombsiteA", "CTSpawn"],
    })
    stats = pl.DataFrame({
        "steam_id": [SELF, MATE, EN1, EN2], "name": ["TestNick", "Mate", "Enemy1", "Enemy2"],
        "kills": [24, 15, 20, 10], "deaths": [15, 16, 18, 19],
        "adr": [97.0, 71.0, 86.0, 50.0], "kast": [80.0, 65.0, 70.0, 55.0],
    })
    players = pl.DataFrame({"steamid": [SELF, MATE, EN1, EN2]})
    return DemoTables(map_name=map_name, tick_rate=64.0, players=players, rounds=rounds,
                      kills=kills, utility=utility, positions=positions, player_stats=stats)


def _sync(make_client, con):
    sync_player_matches(make_client(), con, "TestNick")
    # Fixture maç ID'si "m-1"; gerçek dosya adı formatına uyması için yeniden adlandır
    for table in ("matches", "match_player_stats"):
        con.execute(f"UPDATE {table} SET match_id = ? WHERE match_id = 'm-1'", [M1])


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    inbox, processed, tmp = tmp_path / "inbox", tmp_path / "processed", tmp_path / "tmp"
    inbox.mkdir()
    return inbox, processed, tmp


def test_match_id_from_filename():
    assert match_id_from_filename(f"{M1}-1-1.dem.zst") == M1
    assert match_id_from_filename("benim_demom.dem") is None


def test_decompress_zst_and_find_demos(tmp_path):
    inbox, _, tmp = _dirs(tmp_path)
    raw = b"PBDEMS2\x00" + b"x" * 100
    (inbox / f"{M1}-1-1.dem.zst").write_bytes(zstandard.ZstdCompressor().compress(raw))
    (inbox / "notlar.txt").write_text("demo değil")

    found = find_demos(inbox)
    assert [p.name for p in found] == [f"{M1}-1-1.dem.zst"]
    out = decompress(found[0], tmp)
    assert out.name == f"{M1}-1-1.dem" and out.read_bytes() == raw


def test_ingest_writes_tables_and_moves_file(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")

    r = ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables())

    assert r.error is None and r.match_id == M1
    assert (r.rounds, r.kills, r.utility) == (2, 2, 1)
    assert not demo.exists() and (processed / demo.name).exists()
    assert con.execute("SELECT demo_status FROM matches WHERE match_id = ?", [M1]).fetchone()[0] \
        == "processed"
    # Her round'da CT olan takım konumlardan bulunur
    assert con.execute(
        "SELECT round_no, ct_faction FROM rounds WHERE match_id = ? ORDER BY 1", [M1]
    ).fetchall() == [(1, "faction1"), (2, "faction2")]
    # KAST FACEIT skor tablosuna işlenir
    assert con.execute(
        "SELECT kast FROM match_player_stats WHERE match_id = ? AND player_id = 'p-self'", [M1]
    ).fetchone()[0] == 80.0


def test_validation_compares_with_faceit(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")

    r = ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables())

    by_name = {v.nickname: v for v in r.validation}
    assert by_name["TestNick"].ok  # 24/24 kill, ADR 98.4 vs 97
    assert r.validated is True


def test_validation_flags_mismatch(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")

    def wrong_kills(p: Path) -> DemoTables:
        t = make_tables()
        # Mate: FACEIT 15 kill, demo 18 kill → tolerans (±1) dışında
        t.player_stats = t.player_stats.with_columns(
            pl.when(pl.col("name") == "Mate").then(18).otherwise(pl.col("kills")).alias("kills")
        )
        return t

    r = ingest_demo(con, demo, tmp, processed, parser=wrong_kills)

    bad = [v.nickname for v in r.validation if not v.ok]
    assert bad == ["Mate"] and r.validated is False
    # Güvenilmeyen veri DB'den çıkarılır, maç işaretlenir
    assert con.execute("SELECT count(*) FROM kills WHERE match_id = ?", [M1]).fetchone()[0] == 0
    assert con.execute("SELECT demo_status FROM matches WHERE match_id = ?", [M1]).fetchone()[0]         == "mismatch"
    assert con.execute(
        "SELECT count(*) FROM match_player_stats WHERE match_id = ? AND kast IS NOT NULL", [M1]
    ).fetchone()[0] == 0


def test_reingest_replaces_rows(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    for _ in range(2):
        demo = inbox / f"{M1}-1-1.dem"
        demo.write_bytes(b"demo")
        ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables())

    assert con.execute("SELECT count(*) FROM kills").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM rounds").fetchone()[0] == 2


def test_match_found_by_roster_when_filename_has_no_id(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / "isimsiz.dem"
    demo.write_bytes(b"demo")

    import director.ingest.demos as demos_mod
    old = demos_mod.MIN_ROSTER_OVERLAP
    demos_mod.MIN_ROSTER_OVERLAP = 4  # fixture'da kadro 4 kişi
    try:
        r = ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables())
    finally:
        demos_mod.MIN_ROSTER_OVERLAP = old
    assert r.match_id == M1


def test_unknown_match_keeps_file_in_inbox(tmp_path, con):
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / "isimsiz.dem"
    demo.write_bytes(b"demo")

    r = ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables())

    assert r.match_id is None and "maç bulunamadı" in r.error
    assert demo.exists()


def test_parse_error_does_not_stop_other_demos(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    (inbox / "0-bozuk.dem").write_bytes(b"bozuk")
    (inbox / f"{M1}-1-1.dem").write_bytes(b"demo")

    def parser(p: Path) -> DemoTables:
        if "bozuk" in p.name:
            raise ValueError("geçersiz demo")
        return make_tables()

    results = ingest_inbox(con, inbox, processed, tmp, parser=parser)

    assert [r.error is None for r in results] == [False, True]
    assert (inbox / "0-bozuk.dem").exists()


def test_tmp_file_is_deleted_after_ingest(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem.zst"
    demo.write_bytes(zstandard.ZstdCompressor().compress(b"demo"))

    ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables())

    assert not (tmp / f"{M1}-1-1.dem").exists()
    assert (processed / demo.name).exists()


# --- otomatik silme ----------------------------------------------------------

def test_delete_after_removes_validated_demo(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem.zst"
    demo.write_bytes(zstandard.ZstdCompressor().compress(b"demo"))

    r = ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables(),
                    delete_after=True)

    assert r.deleted and r.freed_bytes > 0
    assert not demo.exists() and not (processed / demo.name).exists()
    # Veriler DB'de kaldı
    assert con.execute("SELECT count(*) FROM kills WHERE match_id = ?", [M1]).fetchone()[0] == 2


def test_delete_after_keeps_demo_when_faceit_mismatch(tmp_path, make_client, con):
    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")

    def wrong(p: Path) -> DemoTables:
        t = make_tables()
        t.player_stats = t.player_stats.with_columns(pl.lit(99).alias("kills"))
        return t

    r = ingest_demo(con, demo, tmp, processed, parser=wrong, delete_after=True)

    assert not r.deleted and "FACEIT" in r.kept_reason
    assert (processed / demo.name).exists()


def test_processed_demos_in_db_only_lists_learned_matches(tmp_path, make_client, con):
    from director.ingest.demos import processed_demos_in_db

    _sync(make_client, con)
    inbox, processed, tmp = _dirs(tmp_path)
    demo = inbox / f"{M1}-1-1.dem"
    demo.write_bytes(b"demo")
    ingest_demo(con, demo, tmp, processed, parser=lambda p: make_tables())
    # Verisi DB'de olmayan bir demo da processed/'da dursun
    other = processed / "1-00000000-0000-0000-0000-000000000000-1-1.dem.zst"
    other.write_bytes(b"x")

    learned = processed_demos_in_db(con, processed)

    assert [(f.name, r, k) for f, r, k in learned] == [(demo.name, 2, 2)]
