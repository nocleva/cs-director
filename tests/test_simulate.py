"""Round simülasyonu (analysis/simulate.py): tekrarlanabilirlik, görüş, simetri, tükenme, can, plant."""

from __future__ import annotations

import math

import numpy as np
import pytest

from squadmind.analysis import rating as rt
from squadmind.analysis import simulate as sm

# Test parametreleri: sabit, veri / dosya gerektirmez. Tablolar simetrik (T = CT'ye eşit).
SYM = dict(end_unplanted=(0.0, 0.5), end_planted=(0.0, 0.5))


def always(a, b):
    return True


def never(a, b):
    return False


def unit(name, elo=1500.0, cls="rifle", x=0.0, y=0.0, **kw):
    return sm.Unit(name, elo, sm.walk([(x, y)]), cls, **kw)


def scenario(nt, nc, horizon=1.0, elo_t=1500.0, elo_c=1500.0, cls_t="rifle", cls_c="rifle"):
    return sm.Scenario("de_dust2",
                       tuple(unit(f"t{i}", elo_t, cls_t, 0, i * 10) for i in range(nt)),
                       tuple(unit(f"c{i}", elo_c, cls_c, 1000, i * 10) for i in range(nc)),
                       horizon=horizon)


def test_track_interpolates_holds_and_waits():
    tr = sm.walk([(0, 0), (500, 0)], speed=250.0)
    assert tr.end == pytest.approx(2.0)
    assert tr.at(np.array([0.0, 1.0, 2.0, 5.0]))[:, 0].tolist() == pytest.approx([0, 250, 500, 500])
    waited = sm.walk([(0, 0), (500, 0)], speed=250.0, start_delay=3.0)    # CT rotasyonu: önce tutar
    assert waited.at(np.array([2.0, 3.0, 4.0]))[:, 0].tolist() == pytest.approx([0, 0, 250])
    assert np.isnan(tr.at(np.array([1.0]))[0, 2])                       # z verilmediyse NaN


def test_duel_p_matches_rating_matchup_including_weapon_and_hp():
    """Simülasyonun düello olasılığı onaylı rating.matchup ile aynı sayı (ELO + silah + can)."""
    we = rt.WeaponEffect(np.array([-0.77, 0.83, 0.07]), np.zeros((3, 3)), 1, True)
    model = rt.RatingModel(base_t={"_": 0.0}, beta=np.array([0.047]), cov=np.zeros((1, 1)),
                           use=(False, False, False, True))

    def feat(elo):
        return rt.PlayerFeatures(x=(0.0, 0.0, 0.0, elo / 100), var=(0.0,) * 4, elo=elo, elo_known=True,
                                 share=0.5, share_raw=0.5, adr=70.0, weight=1.0, n_matches=100,
                                 stat_adj=0.0, combined_elo=elo, combined_sd=0.0)

    for elo_t, elo_c, cls_t, cls_c, dist, hp_t, hp_c in [
        (1800, 1500, "rifle", "rifle", 700, 100, 100), (1500, 1800, "awp", "rifle", 1800, 100, 100),
        (1500, 1500, "pistol", "rifle", 300, 100, 100), (2000, 1200, "rifle", "awp", 300, 35, 100),
        (1500, 1500, "rifle", "rifle", 700, 20, 90), (1500, 1500, "rifle", "rifle", 700, 100, 1)]:
        want = rt.matchup(model, "a", "T", "b", None, feat(elo_t), feat(elo_c), weapon=we, cls_a=cls_t,
                          cls_b=cls_c, dist=dist, hp_a=hp_t, hp_b=hp_c).p
        got = float(sm.duel_p(elo_t - elo_c, rt.weapon_x(cls_t, cls_c, dist, simple=True), hp_t, hp_c,
                              0.047, (-0.77, 0.83, 0.07), rt.HP_COEF))
        assert got == pytest.approx(want, abs=1e-9)


def test_same_seed_repeats_and_other_seed_differs():
    scn = scenario(3, 3, horizon=2.0)
    prep = sm.prepare(scn, always)
    a, b = sm.run(prep, sm.DuelParams(**SYM), n=800, seed=5), sm.run(prep, sm.DuelParams(**SYM), n=800, seed=5)
    c = sm.run(prep, sm.DuelParams(**SYM), n=800, seed=6)
    assert a.p_t == b.p_t and a.lo == b.lo and (a.t_death == b.t_death).all()
    assert a.first_duel_pairs == b.first_duel_pairs
    assert a.p_t != c.p_t


def test_no_line_of_sight_means_no_duel():
    res = sm.simulate(scenario(5, 5, horizon=10.0), sm.DuelParams(**SYM), n=500, los=never)
    assert res.first_duel_rate == 0.0 and res.first_duel_pairs == {}
    assert res.t_kills.sum() == 0 and res.ct_kills.sum() == 0 and res.t_death.sum() == 0
    assert res.outcomes["ufuk"] == 1.0
    assert res.p_t == pytest.approx(0.5)            # simetrik ufuk tablosu, fark 0


def test_no_duel_beyond_range():
    scn = sm.Scenario("de_dust2", (unit("t", x=0),), (unit("c", x=sm.MAX_RANGE + 50),), horizon=1.0)
    assert len(sm.prepare(scn, always).pair_i) == 0
    scn2 = sm.Scenario("de_dust2", (unit("t", x=0),), (unit("c", x=sm.MAX_RANGE - 50),), horizon=1.0)
    assert len(sm.prepare(scn2, always).pair_i) > 0


def test_real_geometry_walls_block_duels_and_open_lines_allow():
    from awpy import data
    if not data.mesh_path("de_dust2").exists():
        pytest.skip("Dust2 geometrisi yerelde yok")
    # B site ↔ A site menzil içinde (2.860) ama duvarlar görüşü keser; aynı nokta kendini görür
    wall = sm.Scenario("de_dust2", (sm.Unit("t", 1500, sm.Track([0], [(-1750, 2400, 30)])),),
                       (sm.Unit("c", 1500, sm.Track([0], [(1097, 2649, 100)])),), horizon=2.0)
    assert len(sm.prepare(wall).pair_i) == 0
    open_ = sm.Scenario("de_dust2", (sm.Unit("t", 1500, sm.Track([0], [(1300, 1500, 0)])),),
                        (sm.Unit("c", 1500, sm.Track([0], [(1300, 1500, 0)])),), horizon=2.0)
    assert len(sm.prepare(open_).pair_i) > 0


def test_equal_strength_is_symmetric_and_stronger_side_favoured():
    prm = sm.DuelParams(**SYM, damage=(0.0,))
    eq = sm.simulate(scenario(5, 5, horizon=3.0), prm, n=6000, los=always)
    assert eq.p_t == pytest.approx(0.5, abs=0.03)
    assert eq.t_death.mean() == pytest.approx(eq.ct_death.mean(), abs=0.03)
    strong = sm.simulate(scenario(5, 5, horizon=3.0, elo_t=1900.0), prm, n=3000, los=always)
    mirror = sm.simulate(scenario(5, 5, horizon=3.0, elo_c=1900.0), prm, n=3000, los=always)
    assert strong.p_t > 0.55 and mirror.p_t < 0.45
    assert strong.p_t + mirror.p_t == pytest.approx(1.0, abs=0.05)


def test_one_side_is_wiped_out_and_kills_equal_deaths():
    # Her zaman görüş + yeterince uzun: round'u biri tükenerek bitirir; kill = ölüm (her tekrar)
    res = sm.simulate(scenario(5, 2, horizon=6.0), sm.DuelParams(**SYM), n=3000, los=always)
    assert res.outcomes["ct_elendi"] + res.outcomes["t_elendi"] == pytest.approx(1.0)
    assert res.outcomes["ufuk"] == 0.0 and res.outcomes["plant"] == 0.0
    assert res.t_kills.sum() == pytest.approx(res.ct_death.sum() * 1.0, abs=1e-9)
    assert res.ct_kills.sum() == pytest.approx(res.t_death.sum(), abs=1e-9)
    assert res.p_t > 0.5                      # sayıca üstün taraf
    assert res.t_kill_dist.shape == (5, 3) and res.t_kill_dist.sum(axis=1) == pytest.approx([1.0] * 5)


def test_winner_health_is_clipped_to_one_and_carries_to_next_duel():
    """1 T, 2 CT, hepsi görüyor. Hasar 500 → kazanan 1 canla kalır (negatif can yok); ikinci düello
    o canla oynanır: P(T ikisini de yener) = 0,5 · duel_p(can 1 vs 100) (hasar 0 iken 0,5 · 0,5)."""
    scn = scenario(1, 2, horizon=0.0)
    heavy = sm.run(sm.prepare(scn, always), sm.DuelParams(**SYM, damage=(500.0,), hp_se=0.0, beta_se=0.0),
                   n=40000, seed=3, uncertainty=False)
    p2 = float(sm.duel_p(0.0, np.zeros(3), 1.0, 100.0, 0.047, (0, 0, 0), rt.HP_COEF))
    assert p2 < 0.3
    assert heavy.p_t == pytest.approx(0.5 * p2, abs=0.008)
    clean = sm.run(sm.prepare(scn, always), sm.DuelParams(**SYM, damage=(0.0,)), n=40000, seed=3,
                   uncertainty=False)
    assert clean.p_t == pytest.approx(0.25, abs=0.008)


def test_plant_needs_survival_until_ready_and_uses_planted_table():
    # T 500 birim yürür (2 sn), 3,2 sn kurar → 5,2 sn. CT0 (2. T'siz) yolda gördüğü planter ile düello eder
    walker = sm.Unit("t0", 1500, sm.walk([(0, 0), (500, 0)]), "rifle", plant=True)
    scn = sm.Scenario("de_dust2", (walker,), (unit("c0", x=300), unit("c1", x=5000)), horizon=8.0)

    def los(a, b):          # sadece c0 görür, T x ≥ 250'den sonra
        return bool(b[0] < 1000 and a[0] >= 250)

    prep = sm.prepare(scn, los)
    assert prep.plant_ready[0] == pytest.approx(5.2)
    res = sm.run(prep, sm.DuelParams(**SYM, damage=(0.0,)), n=20000, seed=1, uncertainty=False)
    # Düelloyu kazanırsa (c0 ölür, c1 uzakta hayatta) kurar; kaybederse T tükenir
    assert res.plant_rate == pytest.approx(0.5, abs=0.02)
    assert res.outcomes["t_elendi"] == pytest.approx(0.5, abs=0.02)
    assert res.outcomes["ufuk"] == 0.0
    # Ufuk, kurma bitişinden kısaysa kurulum olmaz
    short = sm.Scenario("de_dust2", scn.t, scn.ct, horizon=4.0)
    res2 = sm.run(sm.prepare(short, los), sm.DuelParams(**SYM, damage=(0.0,)), n=2000, seed=1)
    assert res2.plant_rate == 0.0


def test_result_reports_first_duel_place_and_interval_contains_estimate():
    res = sm.simulate(scenario(2, 2, horizon=1.0), sm.DuelParams(**SYM), n=1000, los=always)
    assert res.first_duel_rate == 1.0 and res.first_duel_time == 0.0
    assert sum(res.first_duel_cells.values()) == pytest.approx(1.0)
    assert sum(res.first_duel_places.values()) == pytest.approx(1.0)
    assert res.lo <= res.p_t <= res.hi
    assert sm.nearest_place("de_dust2", -1700, 2400) == "B Site"


def test_fit_end_table_recovers_direction_and_shrinks():
    rng = np.random.default_rng(0)
    diff = rng.integers(-3, 4, 600)
    y = (rng.random(600) < 1 / (1 + np.exp(-(0.2 + 0.8 * diff)))).astype(float)
    a, b = sm.fit_end_table(diff, y)
    assert 0.5 < b < 1.1 and abs(a - 0.2) < 0.3
    assert sm.fit_end_table([], []) == (0.0, 0.0)
    assert math.isfinite(sm.fit_end_table([1, 1], [1.0, 1.0])[1])      # tek yönlü veri: patlamaz
