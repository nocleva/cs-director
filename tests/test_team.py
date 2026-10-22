"""Takım taktik desenleri ve ekonomi testleri (sentetik Dust2 maçı).

Takım (faction1): A, B, C, P4, P5 · Rakip (faction2): E
Round 1-4: takım T (ct_faction = faction2) · Round 5-6: takım CT
"""

from __future__ import annotations

import polars as pl
import pytest

from director.analysis.economy import (
    _half,
    after_loss_decisions,
    equipment_edge,
    pistol_conversion,
    with_context,
)
from director.analysis.player_metrics import DemoData
from director.analysis.team_tactics import (
    ct_setups,
    lane_summary,
    pattern_summary,
    setup_summary,
    site_summary,
    t_round_patterns,
)

M = "m"
A, B, C, P4, P5, E = "1", "2", "3", "4", "5", "9"
FREEZE = 1000  # her round'un freeze bitişi (tick)


def _pos(rnd, t, player, place, side="T"):
    return {"match_id": M, "round_no": rnd, "tick": FREEZE + int(t * 64),
            "time_in_round": float(t), "player": player, "side": side,
            "x": 0.0, "y": 0.0, "z": 0.0, "place_name": place}


def make_team_data() -> DemoData:
    rounds = pl.DataFrame({
        "match_id": [M] * 6, "round_no": [1, 2, 3, 4, 5, 6],
        "winner_side": ["T", "T", "CT", "CT", "CT", "T"],
        "reason": ["target_bombed", "terrorists_win", "ct_win", "ct_win", "ct_win",
                   "terrorists_win"],
        "ct_faction": ["faction2"] * 4 + ["faction1"] * 2,
        "ct_buy_type": ["pistol", "full", "full", "full", "full", "full"],
        "t_buy_type": ["pistol", "full", "force", "eco", "full", "full"],
        "ct_equipment": [4000, 25000, 25000, 25000, 25000, 25000],
        "t_equipment": [4000, 24000, 12000, 3000, 24000, 24000],
        "bomb_site": ["A", "A", None, None, None, None],
        "freeze_end_tick": [FREEZE] * 6,
        "plant_tick": [FREEZE + 30 * 64, FREEZE + 55 * 64, None, None, None, None],
    })
    team = [A, B, C, P4, P5]
    roster = pl.DataFrame({
        "match_id": [M] * 6, "steam_id": team + [E],
        "faceit_id": [f"f{i}" for i in range(6)], "nickname": team + [E],
        "faction": ["faction1"] * 5 + ["faction2"],
    })
    pos = [
        # R1 rush A: 20. sn'de üç T long'un sonunda → giriş 20 sn
        _pos(1, 10, A, "OutsideLong"), _pos(1, 10, B, "OutsideLong"),
        _pos(1, 10, C, "LongDoors"),
        _pos(1, 20, A, "LongA"), _pos(1, 20, B, "ARamp"), _pos(1, 20, C, "LongA"),
        # R2 default (15-20 sn'de long / short / mid) → long + short split A, giriş 40 sn
        _pos(2, 16, A, "LongDoors"), _pos(2, 16, B, "Catwalk"), _pos(2, 16, C, "TopofMid"),
        _pos(2, 36, A, "LongDoors"), _pos(2, 36, B, "Catwalk"), _pos(2, 38, C, "Catwalk"),
        _pos(2, 40, A, "LongA"), _pos(2, 40, B, "ShortStairs"), _pos(2, 40, C, "ShortStairs"),
        # R3 tünellerden execute B, giriş 50 sn, plant yok
        _pos(3, 16, A, "UpperTunnel"), _pos(3, 16, B, "UpperTunnel"),
        _pos(3, 16, C, "UpperTunnel"),
        _pos(3, 50, A, "BombsiteB"), _pos(3, 50, B, "BombsiteB"), _pos(3, 50, C, "Hole"),
        # R4 eco: spawn'da kaldılar
        _pos(4, 16, A, "TSpawn"), _pos(4, 16, B, "TSpawn"),
        # R5 CT: A'da 2, Mid'de 2, B'de 1
        _pos(5, 20, A, "BombsiteA", "CT"), _pos(5, 20, B, "LongA", "CT"),
        _pos(5, 20, C, "MidDoors", "CT"), _pos(5, 20, P4, "CTSpawn", "CT"),
        _pos(5, 20, P5, "BombsiteB", "CT"),
        # R6 CT: sadece 4 oyuncunun yeri biliniyor → diziliş yok
        _pos(6, 20, A, "BombsiteA", "CT"), _pos(6, 20, B, "LongA", "CT"),
        _pos(6, 20, C, "MidDoors", "CT"), _pos(6, 20, P4, "CTSpawn", "CT"),
    ]
    empty = pl.DataFrame()
    maps = pl.DataFrame({"match_id": [M], "map": ["de_dust2"], "date": [None]})
    return DemoData(rounds, roster, empty, empty, pl.DataFrame(pos), empty, maps)


@pytest.fixture
def d() -> DemoData:
    return make_team_data()


def test_t_round_patterns(d):
    tp = t_round_patterns(d, A).sort("round_no")
    rows = tp.select("round_no", "pattern", "target_site", "default").rows()
    assert rows == [
        (1, "rush", "A", False),
        (2, "split", "A", True),
        (3, "execute", "B", False),
        (4, "girilmedi", None, False),
    ]
    assert tp["commit_time"].to_list()[:3] == [20.0, 40.0, 50.0]
    assert tp["lanes"][1].to_list() == ["long", "short"]


def test_pattern_and_lane_summary(d):
    tp = t_round_patterns(d, A)
    ps = pattern_summary(tp)
    rush = ps.filter(pl.col("pattern") == "rush").row(0, named=True)
    assert (rush["rounds"], rush["wins"], rush["plants"], rush["plant_wins"]) == (1, 1, 1, 1)
    ls = lane_summary(tp)
    assert ls.filter(pl.col("pattern") == "split")["route"].to_list() == ["long + short"]


def test_ct_setup_needs_all_five(d):
    cs = ct_setups(d, A).sort("round_no")
    assert cs["setup"].to_list() == ["2-2-1", None]
    assert setup_summary(cs).rows() == [("2-2-1", 1, 1)]


def test_site_summary(d):
    from director.analysis.team_tactics import team_rounds

    ss = site_summary(team_rounds(d, A))
    a = ss.filter((pl.col("side") == "T") & (pl.col("bomb_site") == "A")).row(0, named=True)
    # İki plant (R1 30 sn, R2 55 sn), ikisi de kazanıldı, 4 T round'unda
    assert (a["plants"], a["wins"], a["side_rounds"], a["plant_median"]) == (2, 2, 4, 42.5)


# --- ekonomi ---------------------------------------------------------------------

def _tr() -> pl.DataFrame:
    """r1 pistol K, r2 force K, r3 full Y, r4 eco Y, r5 full K │ r13 pistol Y, r14 force K,
    r15 full Y (K = kazanıldı, Y = kaybedildi)."""
    rn = [1, 2, 3, 4, 5, 13, 14, 15]
    return pl.DataFrame({
        "match_id": [M] * 8, "round_no": rn,
        "won": [True, True, False, False, True, False, True, False],
        "own_buy": ["pistol", "force", "full", "eco", "full", "pistol", "force", "full"],
        "enemy_buy": ["pistol", "eco", "full", "full", "full", "pistol", "full", "full"],
        "is_pistol": [r in (1, 13) for r in rn],
        "own_equip": [4000, 15000, 25000, 3000, 25000, 4000, 14000, 25000],
        "enemy_equip": [4000, 3000, 25000, 25000, 26000, 4000, 25000, 20000],
    })


def test_half_numbers():
    s = pl.select(_half(pl.Series([1, 12, 13, 24, 25, 27, 28, 30, 31]))).to_series()
    assert s.to_list() == [0, 0, 1, 1, 2, 2, 3, 3, 4]


def test_with_context_stays_within_half():
    ctx = with_context(_tr()).sort("round_no")
    # r5 yarının son kaydı: sonraki round (r13) başka yarıda → boş
    assert ctx.filter(pl.col("round_no") == 5)["next_won"][0] is None
    assert ctx.filter(pl.col("round_no") == 13)["prev_won"][0] is None


def test_pistol_conversion():
    pc = {r["pistol_won"]: r for r in pistol_conversion(_tr()).iter_rows(named=True)}
    assert (pc[True]["halves"], pc[True]["r2_wins"], pc[True]["r3_wins"]) == (1, 1, 0)
    assert (pc[False]["r2_wins"], pc[False]["r3_wins"], pc[False]["r2_force"]) == (1, 0, 1)


def test_after_loss_decisions():
    rows = {r["own_buy"]: r for r in after_loss_decisions(_tr()).iter_rows(named=True)}
    # r4 eco (r3 kaybından sonra): kaybedildi, sonraki r5 full buy kazanıldı
    eco = rows["eco"]
    assert (eco["rounds"], eco["wins"], eco["next_wins"], eco["next_full"]) == (1, 0, 1, 1)
    # r5 full (r4 kaybından sonra): kazanıldı, yarının son kaydı → sonraki yok
    full = rows["full"]
    assert (full["rounds"], full["wins"], full["next_rounds"]) == (1, 1, 0)
    # r14 force (pistol kaybından sonra; pistol round'unun kendisi sayılmaz)
    force = rows["force"]
    assert (force["rounds"], force["wins"], force["next_wins"]) == (1, 1, 0)


def test_equipment_edge_excludes_pistols():
    ee = dict((edge, (n, w)) for edge, n, w in equipment_edge(_tr()).rows())
    assert sum(n for n, _ in ee.values()) == 6
    assert ee["biz çok öndeyiz (>10k$)"] == (1, 1)  # r2: 15k - 3k
    assert ee["rakip çok önde (>10k$)"] == (2, 1)   # r4 eco (−22k), r14 force (−11k)
