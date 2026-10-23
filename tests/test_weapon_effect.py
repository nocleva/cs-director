"""Silah etkisi (opsiyonel, varsayılan kapalı): rating.WeaponEffect ve duels._main_class testleri."""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

import polars as pl
import pytest

from squadmind.analysis import duels as du
from squadmind.analysis import rating as rt


def test_main_class_uses_positions_up_to_kill_and_falls_back_to_pistol():
    pos = pl.DataFrame({
        "match_id": ["m"] * 5, "round_no": [1] * 5, "player": ["a", "a", "a", "b", "b"],
        "time_in_round": [2.0, 4.0, 6.0, 2.0, 4.0],
        "active_weapon": ["ak47", "awp", "awp", "knife", "glock"],
    })
    d = SimpleNamespace(positions=pos)
    frame = pl.DataFrame({"_i": [0, 1], "match_id": ["m", "m"], "round_no": [1, 1],
                          "winner": ["a", "a"], "loser": ["b", "c"], "_t": [3.0, 7.0]})
    # t=3: a sadece ak47 tutmuş (awp sonra); t=7: awp 2, ak47 1 -> awp; b tabanca/bıçak, c kayıt yok
    assert du._main_class(d, frame, "winner").to_list() == ["rifle", "awp"]
    assert du._main_class(d, frame, "loser").to_list() == ["pistol", "pistol"]


def test_weapon_x_orientation_and_distance_split():
    assert rt.weapon_x("awp", "rifle", 1500).tolist() == [0.0, 1.0, 0.0]
    assert rt.weapon_x("awp", "rifle", 300).tolist() == [0.0, 0.0, 1.0]
    assert rt.weapon_x("pistol", "rifle", 300).tolist() == [1.0, 0.0, 0.0]
    assert rt.weapon_x("rifle", "pistol", 300).tolist() == [-1.0, 0.0, 0.0]
    assert rt.weapon_x("rifle", "rifle", 300).tolist() == [0.0, 0.0, 0.0]


def test_fit_weapon_effect_recovers_signs_and_matchup_is_off_by_default():
    rng = random.Random(4)
    cls_t, cls_ct, dist, y = [], [], [], []
    for _ in range(3000):
        a, b = rng.choice(["rifle", "pistol", "awp"]), rng.choice(["rifle", "pistol", "awp"])
        d = rng.choice([300.0, 1500.0])
        logit = float(rt.weapon_x(a, b, d) @ [-0.8, 0.8, 0.0])
        cls_t.append(a)
        cls_ct.append(b)
        dist.append(d)
        y.append(1.0 if rng.random() < 1 / (1 + math.exp(-logit)) else 0.0)
    we = rt.fit_weapon_effect(cls_t, cls_ct, dist, y, [0.0] * len(y))
    assert we.n == 3000 and we.beta[0] < -0.5 and we.beta[1] > 0.5 and abs(we.beta[2]) < 0.3

    pop = rt.fit_population([])
    f = rt.player_features(rt.History((), ()), pop, elo=1500)
    model = rt.RatingModel({rt.OTHER: 0.0}, rt.np.zeros(1), rt.np.eye(1), rt.DEFAULT_USE)
    off = rt.matchup(model, "a", "T", "b", None, f, f)
    # silah verilmezse ya da eksik verilirse hiçbir şey değişmez (varsayılan kapalı)
    assert rt.matchup(model, "a", "T", "b", None, f, f, weapon=we).p == off.p
    assert rt.matchup(model, "a", "T", "b", None, f, f, weapon=we, cls_a="rifle", cls_b="pistol").p == off.p
    on = rt.matchup(model, "a", "T", "b", None, f, f, weapon=we, cls_a="rifle", cls_b="pistol", dist=300)
    assert on.p > off.p and on.lo <= on.p <= on.hi
    sw = rt.matchup(model, "b", "CT", "a", None, f, f, weapon=we, cls_a="pistol", cls_b="rifle", dist=300)
    assert sw.p + on.p == pytest.approx(1.0, abs=1e-9)
