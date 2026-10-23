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


def test_weapon_x_simple_orientation_and_distance_split():
    """Eski 3 özellikli model (simple=True) aynen korunur."""
    assert rt.weapon_x("awp", "rifle", 1500, simple=True).tolist() == [0.0, 1.0, 0.0]
    assert rt.weapon_x("awp", "rifle", 300, simple=True).tolist() == [0.0, 0.0, 1.0]
    assert rt.weapon_x("pistol", "rifle", 300, simple=True).tolist() == [1.0, 0.0, 0.0]
    assert rt.weapon_x("rifle", "pistol", 300, simple=True).tolist() == [-1.0, 0.0, 0.0]
    assert rt.weapon_x("rifle", "rifle", 300, simple=True).tolist() == [0.0, 0.0, 0.0]


def test_weapon_x_class_by_band_layout():
    k = len(rt.WEAPON_FEATURES)
    assert k == 5 + 15 and rt.weapon_x("rifle", "rifle", 300, simple=False).tolist() == [0.0] * k
    # tanınmayan sınıf (ör. "other") tüfek sayılır
    assert rt.weapon_x("other", "rifle", 300, simple=False).tolist() == [0.0] * k
    x = rt.weapon_x("awp", "rifle", 300, simple=False)
    names = rt.WEAPON_FEATURES
    assert x[names.index("awp")] == 1 and x[names.index("awp@yakın")] == 1 and x.sum() == 2
    assert rt.weapon_x("awp", "rifle", 800, simple=False)[names.index("awp@orta")] == 1
    assert rt.weapon_x("awp", "rifle", 1100, simple=False)[names.index("awp@uzak")] == 1
    # yön: B'nin silahı ters işaret; aynı sınıf aynı dilimde birbirini götürür
    assert rt.weapon_x("rifle", "smg", 300, simple=False)[names.index("smg@yakın")] == -1
    assert rt.weapon_x("awp", "awp", 300, simple=False).tolist() == [0.0] * k
    # farklı sınıflar: AWP (A) - Scout (B)
    x2 = rt.weapon_x("awp", "scout", 1500, simple=False)
    assert x2[names.index("awp@uzak")] == 1 and x2[names.index("scout@uzak")] == -1


def test_fit_weapon_effect_simple_recovers_signs_and_matchup_is_off_by_default():
    rng = random.Random(4)
    cls_t, cls_ct, dist, y = [], [], [], []
    for _ in range(3000):
        a, b = rng.choice(["rifle", "pistol", "awp"]), rng.choice(["rifle", "pistol", "awp"])
        d = rng.choice([300.0, 1500.0])
        logit = float(rt.weapon_x(a, b, d, simple=True) @ [-0.8, 0.8, 0.0])
        cls_t.append(a)
        cls_ct.append(b)
        dist.append(d)
        y.append(1.0 if rng.random() < 1 / (1 + math.exp(-logit)) else 0.0)
    we = rt.fit_weapon_effect(cls_t, cls_ct, dist, y, [0.0] * len(y), simple=True)
    assert we.n == 3000 and we.beta[0] < -0.5 and we.beta[1] > 0.5 and abs(we.beta[2]) < 0.3
    assert we.simple and len(we.beta) == 3

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


def _synthetic(n: int, seed: int, true: dict[tuple[str, str], float]):
    """Hücre etkisi `true` ((sınıf, dilim) → logit, tüfeğe karşı) olan sentetik düellolar."""
    rng = random.Random(seed)
    cls_t, cls_ct, dist, y = [], [], [], []
    classes = ["rifle", "rifle", "awp", "smg", "scout", "pistol", "heavy"]
    dists = {"yakın": 300.0, "orta": 800.0, "uzak": 1500.0}
    for _ in range(n):
        a, b = rng.choice(classes), rng.choice(classes)
        bn = rng.choice(list(dists))
        logit = true.get((a, bn), 0.0) - true.get((b, bn), 0.0)
        cls_t.append(a)
        cls_ct.append(b)
        dist.append(dists[bn])
        y.append(1.0 if rng.random() < 1 / (1 + math.exp(-logit)) else 0.0)
    return cls_t, cls_ct, dist, y


def test_fit_weapon_effect_class_by_band_recovers_cells_and_shrinks_small_samples():
    true = {("awp", "uzak"): 0.9, ("awp", "yakın"): -0.4, ("smg", "uzak"): -0.6, ("pistol", "yakın"): -0.8,
            ("pistol", "orta"): -0.8, ("pistol", "uzak"): -0.8}
    big = rt.fit_weapon_effect(*_synthetic(8000, 1, true), [0.0] * 8000, simple=False)
    assert big.n == 8000 and not big.simple and len(big.beta) == len(rt.WEAPON_FEATURES)
    for (c, bn), e in true.items():
        est, se = big.cell(c, bn)
        assert abs(est - e) < 3 * se + 0.1, (c, bn, est, e)
    assert big.cell("awp", "uzak")[0] > 0.6 > 0 > big.cell("awp", "yakın")[0]
    assert abs(big.cell("heavy", "orta")[0]) < 0.25          # gerçek etki 0
    # tüm hücreler için olasılık / aralık sırası
    cells = rt.weapon_cells(big)
    assert len(cells) == 15
    p, lo, hi, e = cells[("awp", "uzak")]
    assert lo < p < hi and p > 0.6 and e == pytest.approx(big.cell("awp", "uzak")[0])

    # çok az veri: küçültme etkileri 0'a yakın tutar (uçmaz); örnek artınca açılır
    small = rt.fit_weapon_effect(*_synthetic(40, 2, true), [0.0] * 40, simple=False)
    data = _synthetic(40, 2, true)
    loose = rt.fit_weapon_effect(*data, [0.0] * 40, ridge=1e-3, dev_tau=100.0, simple=False)
    assert (small.beta ** 2).sum() < 0.2 * (loose.beta ** 2).sum()
    assert all(abs(small.cell(c, bn)[0]) < 1.5 for c in ("awp", "smg", "scout", "pistol", "heavy")
               for bn in rt.WEAPON_BANDS)
    empty = rt.fit_weapon_effect([], [], [], [], [], simple=False)
    assert empty.n == 0 and not empty.beta.any()


def test_matchup_weapon_class_by_band_symmetry_and_rifle_rifle_is_neutral():
    true = {("awp", "yakın"): -0.6, ("awp", "uzak"): 0.9}
    we = rt.fit_weapon_effect(*_synthetic(6000, 3, true), [0.0] * 6000, simple=False)
    pop = rt.fit_population([])
    f = rt.player_features(rt.History((), ()), pop, elo=1500)
    model = rt.RatingModel({rt.OTHER: 0.0}, rt.np.zeros(1), rt.np.eye(1), rt.DEFAULT_USE)
    off = rt.matchup(model, "a", "T", "b", None, f, f)
    rr = rt.matchup(model, "a", "T", "b", None, f, f, weapon=we, cls_a="rifle", cls_b="rifle", dist=300)
    assert rr.p == off.p                                   # tüfek–tüfek = etki yok
    far = rt.matchup(model, "a", "T", "b", None, f, f, weapon=we, cls_a="awp", cls_b="rifle", dist=1500)
    near = rt.matchup(model, "a", "T", "b", None, f, f, weapon=we, cls_a="awp", cls_b="rifle", dist=300)
    assert far.p > off.p > near.p
    back = rt.matchup(model, "b", "CT", "a", None, f, f, weapon=we, cls_a="rifle", cls_b="awp", dist=300)
    assert back.p + near.p == pytest.approx(1.0, abs=1e-9)


def test_class_band_record_counts_both_sides_and_filters():
    du_ = pl.DataFrame({
        "winner": ["a", "a", "b", "a"], "loser": ["b", "b", "a", "c"],
        "winner_cls": ["awp", "awp", "rifle", "awp"], "loser_cls": ["rifle", "rifle", "awp", "smg"],
        "band": ["yakın", "yakın", "yakın", "uzak"],
    })
    r = du.class_band_record(du_, player="a")
    rows = {(x["cls"], x["band"]): x for x in r.iter_rows(named=True)}
    assert rows[("awp", "yakın")]["wins"] == 2 and rows[("awp", "yakın")]["losses"] == 1
    assert rows[("awp", "yakın")]["n"] == 3 and rows[("awp", "yakın")]["win_rate"] == pytest.approx(2 / 3)
    assert rows[("awp", "uzak")]["wins"] == 1 and rows[("awp", "uzak")]["n"] == 1
    # b'nin tüfeği: 2 yenilgi (a'ya) + 1 galibiyet
    rb = du.class_band_record(du_, player="b").filter(pl.col("cls") == "rifle")
    assert rb["wins"].to_list() == [1] and rb["losses"].to_list() == [2]
    # rakip sınıfı süzgeci: a'nın AWP'si SMG'ye karşı yalnız uzakta 1-0
    rs = du.class_band_record(du_, player="a", vs_cls="smg")
    assert rs.height == 1 and rs["band"][0] == "uzak"
    assert du.class_band_record(du_.head(0)).height == 0
    assert du.class_band_record(du_)["player"].n_unique() == 3
