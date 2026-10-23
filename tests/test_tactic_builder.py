"""Taktik oluşturucu yardımcıları (analysis/tactic_builder.py) testleri."""

from __future__ import annotations

import polars as pl

from squadmind.analysis import tactic_builder as tb
from squadmind.analysis.player_metrics import DemoData


def _d(rows):
    positions = pl.DataFrame(rows, schema=["x", "y", "z", "place_name"], orient="row")
    empty = pl.DataFrame()
    return DemoData(rounds=empty, roster=empty, kills=empty, utility=empty, positions=positions,
                    stats=empty, match_maps=empty)


def test_place_grid_majority_and_nearest_lookup():
    d = _d([(10, 10, 0, "LongA"), (20, 15, 0, "LongA"), (30, 20, 0, "Pit"),
            (1000, 1000, 50, "BombsiteB")])
    grid = tb.place_grid(d)
    assert ("LongA" in {g[3] for g in grid}) and len(grid) == 2   # aynı hücrede çoğunluk
    assert tb.place_at(grid, 0, 0)[0] == "LongA"
    assert tb.place_at(grid, 1010, 990)[0] == "BombsiteB"
    assert tb.place_at(grid, 5000, 5000) == (None, None)         # çok uzak: bilinmiyor


def test_lines_for_place_and_cards():
    assert ("long", "Long Doors ↔ Long A") in tb.lines_for_place("de_dust2", "LongA")
    assert tb.lines_for_place("de_dust2", None) == []
    model = {"overall": {"s1": (0.6, 40)},
             "lines": {("s1", "long", "T"): {"n": 12, "wins": 8, "strength": 0.61234, "conf": "orta"}}}
    cards = tb.player_cards(model, "de_dust2", ["s1", "s2"])
    assert cards["s1"]["lines"]["long|T"]["strength"] == 0.612
    assert cards["s2"] == {"overall": None, "duels": 0, "lines": {}}
