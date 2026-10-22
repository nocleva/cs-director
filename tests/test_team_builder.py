"""Takım oluşturma sorguları (analysis/team_builder.py) testleri: küçük elle kurulmuş DB."""

from __future__ import annotations

import pytest

from squadmind.analysis import team_builder as tb
from squadmind.db import connect, init_db


@pytest.fixture
def db():
    con = connect(":memory:")
    init_db(con)
    # m1: A, B, C aynı tarafta (kazandı), D rakip · m2: A, B aynı (kaybetti), C rakip · m3: A tek
    rows = [
        ("m1", "A", "faction1", True), ("m1", "B", "faction1", True), ("m1", "C", "faction1", True),
        ("m1", "D", "faction2", False),
        ("m2", "A", "faction1", False), ("m2", "B", "faction1", False), ("m2", "C", "faction2", True),
        ("m3", "A", "faction1", True),
    ]
    for mid, pid, fac, res in rows:
        con.execute("INSERT INTO match_player_stats (match_id, player_id, nickname, faction, result, "
                    "kills, deaths, adr, hs_pct) VALUES (?, ?, ?, ?, ?, 20, 10, 80, 50)",
                    [mid, pid, f"nick{pid}", fac, res])
    for mid, mp, st in (("m1", "de_dust2", "processed"), ("m2", "de_dust2", "processed"),
                        ("m3", "de_mirage", None)):
        con.execute("INSERT INTO matches (match_id, map, demo_status) VALUES (?, ?, ?)", [mid, mp, st])
    con.execute("INSERT INTO players (faceit_id, nickname, level) VALUES ('C', 'Nicorette', 9)")
    yield con
    con.close()


def test_demo_level_thresholds():
    assert [tb.demo_level(n) for n in (0, 5, 6, 9, 10, 25)] == ["low", "low", "mid", "mid", "ok", "ok"]


def test_played_with_counts_same_side_only(db):
    rows = tb.played_with(db, "A", min_together=1)
    assert [(r["faceit_id"], r["together"], r["wins"]) for r in rows] == [("B", 2, 1), ("C", 1, 1)]
    assert [r["faceit_id"] for r in tb.played_with(db, "A", exclude=["B"], min_together=1)] == ["C"]
    # Varsayılan: tek maçlık oyuncu öneri değil
    assert [r["faceit_id"] for r in tb.played_with(db, "A")] == ["B"]


def test_search_ranks_exact_then_prefix(db):
    names = [r["nickname"] for r in tb.search_players(db, "nick")]
    assert names[0].lower().startswith("nick")
    assert tb.search_players(db, "nicorette")[0]["faceit_id"] == "C"
    assert tb.search_players(db, "n") == []   # tek harf aranmaz


def test_pair_matrix_includes_pairs_without_matches(db):
    m = {(r["a"], r["b"]): (r["together"], r["wins"]) for r in tb.pair_matrix(db, ["A", "B", "D"])}
    assert m[("A", "B")] == (2, 1)
    assert m[("A", "D")] == (0, 0) and m[("B", "D")] == (0, 0)


def test_team_maps_and_demo_counts(db):
    maps = tb.team_maps(db, ["A", "B", "C"])
    assert maps == [{"map": "de_dust2", "matches": 2, "wins": 1, "most": 3}]
    assert tb.demo_counts(db, ["A", "D"]) == {"A": {"de_dust2": 2}, "D": {"de_dust2": 1}}


def test_member_stats(db):
    st = tb.member_stats(db, ["A"])["A"]
    assert st["matches"] == 3 and st["kd"] == 2 and round(st["win"], 2) == 0.67
