"""Takım yönetimi analizi testleri (test_team'deki sentetik maç + kill olayları).

Takım (faction1): A, B, C, P4, P5 · Rakip (faction2): E
Round 1-4: takım T · Round 5-6: takım CT
"""

from __future__ import annotations

import polars as pl
import pytest

from director.analysis.team_management import (
    OTHER,
    core_roster,
    player_spots,
    proximity_pairs,
    spot_ranking,
    support_needs,
    trade_pairs,
)
from tests.test_team import A, B, C, E, FREEZE, M, make_team_data


def _kill(rnd, t, att, att_side, vic, vic_side, opening=False, traded=False):
    return {"match_id": M, "round_no": rnd, "tick": FREEZE + int(t * 64),
            "time_in_round": float(t), "attacker": att, "attacker_side": att_side,
            "victim": vic, "victim_side": vic_side, "weapon": "ak47", "headshot": False,
            "is_opening": opening, "is_trade_kill": False, "is_traded": traded,
            "attacker_place": "LongA", "victim_place": "LongA"}


@pytest.fixture
def d():
    d = make_team_data()
    d.kills = pl.DataFrame([
        # R5 (takım CT): E, B'yi öldürür; A 2 sn sonra E'yi → A, B'yi trade etti
        _kill(5, 20, E, "T", B, "CT", opening=True, traded=True),
        _kill(5, 22, A, "CT", E, "T"),
        # R6: E, C'yi öldürür; A 8 sn sonra E'yi öldürür → trade sayılmaz
        _kill(6, 20, E, "T", C, "CT", opening=True),
        _kill(6, 28, A, "CT", E, "T"),
    ])
    return d


def test_core_roster_is_player_and_frequent_mates(d):
    roster = core_roster(d, A)
    # Tek maç: herkes maçların yarısından fazlasında birlikte; rakip E yok
    assert set(roster["steam_id"].to_list()) == {"1", "2", "3", "4", "5"}
    assert roster.filter(pl.col("steam_id") == A)["matches"][0] == 1


def test_trade_pairs_only_within_window(d):
    assert trade_pairs(d, A).rows() == [(A, B, 1)]


def test_player_spots_and_ranking(d):
    sp = player_spots(d, A)
    a_ct = sp.filter((pl.col("side") == "CT") & (pl.col("spot") == "A")
                     & (pl.col("player") == A)).row(0, named=True)
    # A iki CT round'unda da A alanında; R5 kazanıldı, iki kill, ölüm yok
    assert (a_ct["rounds"], a_ct["wins"], a_ct["kills"], a_ct["deaths"]) == (2, 1, 2, 0)
    # Lobi satırı aynı maçlardaki herkesi içerir
    assert "lobi" in sp["player"].to_list()

    ranked = spot_ranking(sp)
    # B (CT, A alanı / LongA): 2 round, 0 kill, 1 ölüm → net −0,5 (işaretsiz taşma olmadan)
    b_ct = ranked.filter((pl.col("side") == "CT") & (pl.col("player") == B)).row(0, named=True)
    assert b_ct["net"] == pytest.approx(-0.5)
    a_rank = ranked.filter((pl.col("side") == "CT") & (pl.col("spot") == "A")
                           & (pl.col("player") == A))["rank"][0]
    assert a_rank == 1
    assert ranked.filter(pl.col("player") == "lobi")["rank"].null_count() > 0


def test_support_needs_counts_traded_and_opening(d):
    sn = support_needs(d, A)
    b = sn.filter((pl.col("player") == B) & (pl.col("side") == "CT")).row(0, named=True)
    assert (b["deaths"], b["traded"], b["opening_deaths"], b["rounds"]) == (1, 1, 1, 2)


def test_proximity_pairs(d):
    pp = proximity_pairs(d, A)
    # Testte herkes (0,0,0)'da: ikisi de hayattayken her kayıt yakın
    ab = pp.filter((pl.col("side") == "CT") & (pl.col("a") == A) & (pl.col("b") == B))
    assert ab["samples"][0] == ab["near"][0] == 2


def test_rare_mates_are_grouped_as_other():
    from director.analysis.team_management import _team_player_rounds

    d = make_team_data()
    # A 3 maç oynar; X sadece m2'de, B..P5 sadece m'de → 1/3 maç: çekirdek değil
    extra = pl.DataFrame({"match_id": ["m2", "m2", "m3"], "steam_id": [A, "X", A],
                          "faceit_id": ["fa", "fx", "fa"], "nickname": [A, "X", A],
                          "faction": ["faction1"] * 3})
    d.roster = pl.concat([d.roster, extra])
    r1 = d.rounds.filter(pl.col("round_no") == 1)
    d.rounds = pl.concat([d.rounds, r1.with_columns(pl.lit("m2").alias("match_id")),
                          r1.with_columns(pl.lit("m3").alias("match_id"))])

    assert core_roster(d, A)["steam_id"].to_list() == [A]
    players = set(_team_player_rounds(d, A)["player"].to_list())
    assert players == {A, OTHER}


def test_early_death_still_gets_a_spot():
    """15. sn'den önce ölen oyuncu pencere dışında kalmaz: son kaydının alanı verilir."""
    from tests.test_team import _pos

    d = make_team_data()
    # P5 R6'da sadece 5. sn'de B'de görülüyor (pencere 15-35 sn başlamadan öldü)
    d.positions = pl.concat([d.positions, pl.DataFrame([_pos(6, 5, "5", "BombsiteB", "CT")])])
    d.kills = pl.DataFrame([_kill(6, 6, E, "T", "5", "CT", opening=True)])
    sp = player_spots(d, A)
    p5 = sp.filter((pl.col("player") == "5") & (pl.col("side") == "CT")
                   & (pl.col("spot") == "B")).row(0, named=True)
    # R5'te de B'deydi: 2 round, 1 ölüm (R6'daki erken opening ölümü sayıldı)
    assert (p5["rounds"], p5["deaths"], p5["od"]) == (2, 1, 1)
