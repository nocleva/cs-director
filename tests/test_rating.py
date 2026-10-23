"""Oyuncu gücü + karşılaşma olasılığı (analysis/rating.py) testleri."""

from __future__ import annotations

import math
import random

import httpx
import polars as pl
import pytest

from squadmind.analysis import rating as rt


def _hist(share: float, n: int, adr: float = 80.0) -> rt.History:
    """n maç, her maçta 20 temas, kill payı `share` (deterministik)."""
    k = round(share * 20)
    return rt.History(tuple([k / 20] * n), tuple([adr] * n), k * n, (20 - k) * n)


def _noisy_pop(seed: int = 1) -> rt.Population:
    """Seviyesi yükseldikçe kill payı hafif artan, maçtan maça oynayan sentetik popülasyon."""
    rng = random.Random(seed)
    players = []
    for i in range(120):
        level = 1 + i % 10
        elo = rt.level_elo(level)[0]
        true = 0.44 + 0.006 * level + rng.gauss(0, 0.02)
        shares = tuple(min(max(rng.gauss(true, 0.1), 0.05), 0.95) for _ in range(60))
        kills = round(sum(shares) * 20)
        players.append((rt.History(shares, tuple([75.0] * 60), kills, 60 * 20 - kills), elo))
    return rt.fit_population(players)


POP = _noisy_pop()


def test_summarize_history_skips_bad_rows_and_sums():
    rows = [{"Kills": "10", "Deaths": "10", "ADR": "80"}, {"Kills": "15", "Deaths": "5", "ADR": None},
            {"Kills": None, "Deaths": "3"}, {"Kills": "0", "Deaths": "0", "ADR": "0"}]
    h = rt.summarize_history(rows)
    assert h.n == 2 and h.kills == 25 and h.deaths == 15
    assert h.share == pytest.approx(25 / 40)
    assert h.adr == 80.0                      # ADR'sız satır ADR ortalamasına girmez
    assert rt.summarize_history([]).share is None


def test_level_elo_midpoints_and_unknown():
    assert rt.level_elo(9) == (1875.5, pytest.approx((2000 - 1751) ** 2 / 12))
    assert rt.level_elo(0) == (None, 0.0)
    assert rt.level_elo(None) == (None, 0.0)


def test_population_learns_level_slope_and_positive_tau():
    assert POP.share.b > 0                    # seviye ↑ → beklenen kill payı ↑
    assert 0 < POP.share.tau2 < POP.share.s2  # gerçek fark maç içi oynamadan küçük


def test_shrinkage_weight_grows_with_matches_and_no_data_means_prior():
    elo = rt.level_elo(5)[0]
    mu = POP.share.a + POP.share.b * elo
    v0, _, w0 = POP.share.shrink(None, 0, elo)
    assert v0 == pytest.approx(mu) and w0 == 0
    ws = [POP.share.shrink(0.6, n, elo)[2] for n in (1, 5, 20, 100)]
    assert ws == sorted(ws) and ws[0] < 0.2 and ws[-1] > 0.7
    # 3 maçta %70 kill payı neredeyse seviyesine çekilir; 100 maçta büyük ölçüde korunur
    assert POP.share.shrink(0.7, 3, elo)[0] < POP.share.shrink(0.7, 100, elo)[0] < 0.7


def test_stat_adjustment_is_bounded_and_needs_known_elo():
    hi = rt.player_features(_hist(0.95, 100), POP, level=3, kappa=10.0)
    lo = rt.player_features(_hist(0.05, 100), POP, level=10, kappa=10.0)
    assert 0 < hi.stat_adj <= rt.MAX_STAT_ADJ and -rt.MAX_STAT_ADJ <= lo.stat_adj < 0
    unknown = rt.player_features(_hist(0.9, 100), POP)
    assert unknown.stat_adj == 0 and not unknown.elo_known
    assert unknown.combined_sd > hi.combined_sd   # bilinmeyen ELO → geniş belirsizlik
    exact = rt.player_features(_hist(0.5, 100), POP, elo=1900, level=3)
    assert exact.elo == 1900                      # gerçek ELO seviyeye üstün


def test_bound_is_identity_near_zero_and_never_exceeds_limit():
    assert rt.bound(0.05) == pytest.approx(0.05, rel=1e-3)
    assert abs(rt.bound(50.0)) <= rt.MAX_LOGIT and abs(rt.bound(-50.0)) <= rt.MAX_LOGIT
    assert rt.bound(2.0) < 2.0 * 0.85            # büyük fark belirgin kısılır


def _synthetic_duels(n_matches: int = 12, seed: int = 3) -> tuple[pl.DataFrame, dict]:
    """Seviye farkı düello sonucunu gerçekten etkileyen sentetik düellolar."""
    rng = random.Random(seed)
    levels = {f"p{i}": 1 + i % 10 for i in range(20)}
    rows = []
    for m in range(n_matches):
        for _ in range(150):
            a, b = rng.sample(sorted(levels), 2)
            diff = (rt.level_elo(levels[a])[0] - rt.level_elo(levels[b])[0]) / 100
            a_wins = rng.random() < 1 / (1 + math.exp(-0.08 * diff))
            w, lo = (a, b) if a_wins else (b, a)
            rows.append({"match_id": f"m{m}", "round_no": 1, "winner": w, "loser": lo,
                         "winner_side": "T", "loser_side": "CT", "line": "long", "band": "orta"})
            rows[-1]["winner_side"], rows[-1]["loser_side"] = rng.choice((("T", "CT"), ("CT", "T")))
    feats = {p: rt.player_features(_hist(0.5, 50), POP, level=lv) for p, lv in levels.items()}
    return pl.DataFrame(rows), feats


def test_fit_model_recovers_level_effect_and_matchup_is_symmetric_and_bounded():
    du, feats = _synthetic_duels()
    model = rt.fit_model(du, lambda _m, s: feats[s])
    assert 0.04 < model.beta[0] < 0.12               # gerçek 0,08
    hi, lo = feats["p9"], feats["p0"]                 # seviye 10 vs seviye 1
    m1 = rt.matchup(model, "p9", "T", "p0", "long", hi, lo)
    m2 = rt.matchup(model, "p0", "CT", "p9", "long", lo, hi)
    assert m1.p > 0.6 and m1.p + m2.p == pytest.approx(1)
    assert m1.lo < m1.p < m1.hi and m1.conf == "belirgin"
    assert 0.2 < m2.p and m1.p < 0.8                  # tanh sınırı
    same = rt.matchup(model, "p3", "T", "p13", None, feats["p3"], feats["p13"])
    assert same.p == pytest.approx(0.5) and same.conf == "belirsiz"


def test_few_matches_or_unknown_level_is_labelled_low_data():
    du, feats = _synthetic_duels(4)
    model = rt.fit_model(du, lambda _m, s: feats[s])
    rookie = rt.player_features(_hist(0.6, 5), POP, level=9)
    nobody = rt.player_features(rt.History((), ()), POP)
    assert rt.matchup(model, "x", "T", "p0", "long", rookie, feats["p0"]).conf == "az veri"
    r = rt.matchup(model, "x", "T", "p0", "long", nobody, feats["p0"])
    assert r.conf == "az veri" and r.n["faceit_maç_a"] == 0


def test_optional_demo_and_line_residuals_shrink_small_samples():
    du, feats = _synthetic_duels(6)
    model = rt.fit_model(du, lambda _m, s: feats[s], demo=True, line=True, base="line")
    assert model.delta and model.lam and "long" in model.base_t
    # Artıklar ön bilgi sapmasının birkaç katını geçmez (veri seviye etkisiyle zaten açıklanıyor)
    assert max(abs(v[0]) for v in model.delta.values()) < 4 * rt.TAU_DEMO
    r = rt.matchup(model, "p1", "T", "p2", "long", feats["p1"], feats["p2"], band="orta")
    assert r.n["demo_düello_a"] > 0 and r.n["hat_düello_a"] > 0


def test_rating_score_is_bounded_and_ordered():
    du, feats = _synthetic_duels()
    model = rt.fit_model(du, lambda _m, s: feats[s])
    s_hi = rt.rating_score(model, feats["p9"])
    s_lo = rt.rating_score(model, feats["p0"])
    assert 20 < s_lo[0] < 50 < s_hi[0] < 80
    assert s_hi[1] <= s_hi[0] <= s_hi[2]


def test_metrics():
    assert rt.log_loss([0.5, 0.5], [1, 0]) == pytest.approx(math.log(2))
    assert rt.brier([1.0, 0.0], [1, 0]) == 0
    cal = rt.calibration([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], bins=2)
    assert cal == [{"n": 2, "pred": pytest.approx(0.15), "obs": 0.0},
                   {"n": 2, "pred": pytest.approx(0.85), "obs": 1.0}]


def test_player_match_stats_uses_millisecond_cutoff_and_cache(make_client, recorder):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/players/p1/games/cs2/stats")
        return httpx.Response(200, json={"items": [{"stats": {"Match Id": "m2", "Kills": "5"}}]})

    client = make_client(handler)
    items = client.get_player_match_stats("p1", to_ms=1_700_000_000_000)
    client.get_player_match_stats("p1", to_ms=1_700_000_000_000)
    assert items == [{"Match Id": "m2", "Kills": "5"}]
    assert len(recorder.requests) == 1 and recorder.requests[0].url.params["to"] == "1700000000000"


class _FakeClient:
    """build_context için: her oyuncunun geçmişi, maç listesi içinde hedef maçı da içerir."""

    def get_player_match_stats(self, faceit_id, to_ms=None, limit=100):
        lv = int(faceit_id[1:])
        row = {"Kills": str(10 + lv), "Deaths": "15", "ADR": "80", "Match Finished At": 0}
        return [{"Match Id": "m0", **row}] + [{"Match Id": f"old{i}", **row} for i in range(30)]

    def get_player(self, faceit_id):
        return {"nickname": f"N{faceit_id}", "games": {"cs2": {"faceit_elo": 1500, "skill_level": 7}}}


def test_build_context_uses_only_pre_match_history():
    du, _ = _synthetic_duels(2)
    du = du.with_columns(pl.lit("m0").alias("match_id"))
    roster = [{"match_id": "m0", "steam_id": f"p{i}", "faceit_id": f"f{i}", "level": 1 + i % 10,
               "started_at": 1, "finished_at": 2} for i in range(20)]
    ctx = rt.build_context(du, _FakeClient(), roster, [{"steam_id": "p1", "faceit_id": "f1"},
                                                       {"steam_id": "p2", "faceit_id": "f2"}])
    assert ctx.features["p1"].n_matches == 31 and ctx.features["p1"].elo == 1500
    assert ctx.nicknames["p2"] == "Nf2"
    r = ctx.matchup("p2", "T", "p1", "long")
    assert r is not None and r.p > 0.5               # aynı ELO, p2'nin kill payı daha yüksek
    assert ctx.matchup("p2", "T", "yok", "long") is None
