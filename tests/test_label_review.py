"""Desen doğrulama (analysis/label_review.py) testleri: örnek seçimi, tekrar verisi, doğruluk."""

from __future__ import annotations

import polars as pl

from squadmind.analysis import label_review as lr
from squadmind.analysis.player_metrics import DemoData


def _tp(n_rush: int, n_split: int) -> pl.DataFrame:
    rows = [{"match_id": f"m{i}", "round_no": i, "pattern": "rush"} for i in range(n_rush)]
    rows += [{"match_id": f"s{i}", "round_no": i, "pattern": "split"} for i in range(n_split)]
    return pl.DataFrame(rows)


def test_sample_rounds_is_deterministic_interleaved_and_capped():
    tp = _tp(9, 2)
    a, b = lr.sample_rounds(tp, per_pattern=3), lr.sample_rounds(tp, per_pattern=3)
    assert a == b                                    # her açılışta aynı round'lar
    assert [r["pattern"] for r in a] == ["rush", "split", "rush", "split", "rush"]
    assert len({(r["match_id"], r["round_no"]) for r in a}) == 5


def test_round_replay_projects_points_and_kills():
    positions = pl.DataFrame({
        "match_id": ["m", "m", "m"], "round_no": [1, 1, 1], "tick": [1, 2, 3],
        "time_in_round": [0.0, 2.0, 0.0], "player": ["a", "a", "b"], "side": ["T", "T", "CT"],
        "x": [0.0, 10.0, 5.0], "y": [0.0, 0.0, 5.0]})
    kills = pl.DataFrame({"match_id": ["m"], "round_no": [1], "time_in_round": [3.0],
                          "victim": ["b"], "victim_side": ["CT"], "victim_x": [5.0], "victim_y": [5.0]})
    roster = pl.DataFrame({"match_id": ["m", "m"], "steam_id": ["a", "b"], "nickname": ["Ali", "Bob"]})
    d = DemoData(rounds=pl.DataFrame(), roster=roster, kills=kills, utility=pl.DataFrame(),
                 positions=positions, stats=pl.DataFrame(), match_maps=pl.DataFrame())
    rep = lr.round_replay(d, "m", 1, lambda x, y: (x * 2, y * 2))
    ali = next(p for p in rep["players"] if p["name"] == "Ali")
    assert ali["side"] == "T" and ali["pts"] == [[0.0, 0.0, 0.0], [2.0, 20.0, 0.0]]
    assert rep["kills"] == [{"t": 3.0, "x": 10.0, "y": 10.0, "victim_side": "CT", "victim": "Bob"}]
    assert rep["duration"] == 3.0


def test_accuracy_ignores_unsure_and_counts_fixes():
    reviews = [{"label": "rush", "verdict": "dogru"}, {"label": "rush", "verdict": "yanlis",
                                                      "corrected": "execute"},
               {"label": "rush", "verdict": "emin_degil"}]
    rush = next(a for a in lr.accuracy(reviews) if a["pattern"] == "rush")
    assert (rush["reviewed"], rush["correct"], rush["fixes"]) == (2, 1, {"execute": 1})
