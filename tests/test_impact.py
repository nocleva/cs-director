"""Kill / hasar değeri (analysis/impact.py) testleri.

test_player_metrics'teki küçük maç kullanılır (2'ye 2, 4 round) ve elle hesaplanabilir
bir hasar tablosu eklenir. Sağ kalan sayısı 5 kişilik takım varsayımıyla hesaplanır.
"""

from __future__ import annotations

import polars as pl
import pytest

from squadmind.analysis.impact import (
    damage_outcomes,
    impact_counts,
    impact_rates,
    kill_table,
    kill_value_by_weapon,
    win_prob_table,
)
from tests.test_player_metrics import A, B, C, D, M, make_data


def _dmg(rnd, t, att, att_side, vic, vic_side, dmg, weapon="ak47"):
    return {"match_id": M, "round_no": rnd, "tick": int(t * 64), "attacker": att,
            "attacker_side": att_side, "victim": vic, "victim_side": vic_side,
            "weapon": weapon, "dmg": dmg}


@pytest.fixture
def d():
    d = make_data()
    d.damages = pl.DataFrame([
        # R1: B, C'yi 70 hasarla low bırakır; A 30 ile bitirir (AWP)
        _dmg(1, 5, B, "CT", C, "T", 70),
        _dmg(1, 10, A, "CT", C, "T", 30, "awp"),
        # R1: D, B'yi tek başına indirir; A, D'yi iki isabetle tek başına indirir
        _dmg(1, 20, D, "T", B, "CT", 100),
        _dmg(1, 15, A, "CT", D, "T", 50, "ssg08"),
        _dmg(1, 22, A, "CT", D, "T", 50, "ssg08"),
        # R2: C, A'yı indirir
        _dmg(2, 30, C, "T", A, "CT", 100),
        # R3: A (T), C'yi tek atış
        _dmg(3, 8, A, "T", C, "CT", 100, "awp"),
        # R4: B (T), D'ye 25 hasar; D round'u sağ bitirir. D, B'yi indirir
        _dmg(4, 50, B, "T", D, "CT", 25),
        _dmg(4, 70, D, "CT", B, "T", 100),
    ])
    return d


def test_win_prob_table_smoothing(d):
    wp = win_prob_table(d)
    # 5v5: her round her taraf için bir gözlem. CT 4 round'un 3'ünü kazandı.
    # Birleşik (iki taraf) 8 gözlem, 4 galibiyet → (4+1)/(8+2) = 0,5
    ct55 = wp.filter((pl.col("side") == "CT") & (pl.col("own") == 5)
                     & (pl.col("enemy") == 5)).row(0, named=True)
    assert ct55["n"] == 4
    assert ct55["p"] == pytest.approx((3 + 10 * 0.5) / (4 + 10))
    assert wp["p"].is_between(0, 1).all()


def test_kill_table_type_state_and_wpa(d):
    k = kill_table(d)
    # Takım içi kill (R4 B → A) yok
    assert k.height == 6
    r1 = k.filter((pl.col("round_no") == 1) & (pl.col("attacker") == A)).sort("tick")
    first, second = r1.row(0, named=True), r1.row(1, named=True)
    # A'nın C kill'i: 30 hasar → bitirme; 5v5 → opening
    assert (first["kill_type"], first["killer_dmg"]) == ("bitirme", 30)
    assert (first["own_alive"], first["enemy_alive"]) == (5, 5)
    assert first["wpa"] > 0
    assert (first["own_buy"], first["enemy_buy"]) == ("pistol", "pistol")
    # A'nın D kill'i: iki isabet 50+50 → tek başına; öncesinde B ve C ölmüş → 4v4
    assert (second["kill_type"], second["killer_dmg"]) == ("solo", 100)
    assert (second["own_alive"], second["enemy_alive"]) == (4, 4)


def test_damage_outcomes(d):
    o = damage_outcomes(d)

    def outcome(att, vic, rnd):
        return o.filter((pl.col("attacker") == att) & (pl.col("victim") == vic)
                        & (pl.col("round_no") == rnd)).row(0, named=True)

    b_on_c = outcome(B, C, 1)
    assert (b_on_c["outcome"], b_on_c["my_dmg"], b_on_c["hp_after"]) == ("takım", 70, 30)
    assert outcome(A, D, 1)["outcome"] == "ben"
    b_on_d = outcome(B, D, 4)
    assert (b_on_d["outcome"], b_on_d["hp_after"]) == ("hayatta", 75)


def test_impact_counts_and_rates(d):
    ic = impact_counts(d)
    a = ic.filter(pl.col("steam_id") == A).row(0, named=True)
    assert (a["kills"], a["solo_kills"], a["finish_kills"], a["rounds"]) == (3, 2, 1, 4)
    b = ic.filter(pl.col("steam_id") == B).row(0, named=True)
    # B: C'ye 70 hasar, C'yi A bitirdi → asist, low bırakma, takım bitirdi
    assert (b["assists"], b["low_left"], b["low_converted"], b["support_dmg"]) == (1, 1, 1, 70)
    # B'nin hasarı 95; 70'i ölen rakibe gitti
    assert (b["dmg_total"], b["dmg_to_dead"]) == (95, 70)

    r = impact_rates(ic.filter(pl.col("steam_id") == A))
    assert r["solo_kill_share"].value == pytest.approx(2 / 3)
    assert r["wpa_per_round"].den == 4
    # Ekipman verisi yoksa oran tanımsız (0/0), hata vermez
    assert r["equip_per_kill"].value is None


def test_kill_value_by_weapon(d):
    kv = kill_value_by_weapon(d, A)
    awp = kv.filter(pl.col("weapon") == "awp").row(0, named=True)
    assert (awp["kills"], awp["solo"], awp["finish"]) == (2, 1, 1)
    scout = kv.filter(pl.col("weapon") == "scout").row(0, named=True)
    assert (scout["kills"], scout["solo"]) == (1, 1)


def test_without_damage_data_kill_type_is_unknown():
    d = make_data()  # hasar tablosu yok (eski parser)
    k = kill_table(d)
    assert k["kill_type"].null_count() == k.height
    r = impact_rates(impact_counts(d))
    assert r["solo_kill_share"].value is None
