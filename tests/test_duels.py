"""Peek hattı düelloları ve güç modeli (analysis/duels.py) testleri."""

from __future__ import annotations

import polars as pl

from squadmind.analysis import duels as du


def _duels(rows):
    keys = ("winner", "loser", "winner_side", "loser_side", "line")
    data = [dict(zip(keys, r, strict=True)) | {"band": "orta"} for r in rows]
    return pl.DataFrame(data)


def test_line_of_is_direction_free_and_first_match_wins():
    assert du.line_of("de_dust2", "LongDoors", "LongA") == "long"
    assert du.line_of("de_dust2", "LongA", "LongDoors") == "long"
    assert du.line_of("de_dust2", "LongDoors", "LongDoors") == "long_corner"
    assert du.line_of("de_dust2", "BombsiteB", "BombsiteB") == "b_site"
    assert du.line_of("de_dust2", "Middle", "BombsiteB") is None
    assert du.line_of("de_mirage", "LongDoors", "LongA") is None


def test_band_of():
    assert [du.band_of(x) for x in (100, 499, 500, 1099, 1100, 3000)] == \
        ["yakın", "yakın", "orta", "orta", "uzak", "uzak"]


def test_strengths_shrink_small_samples_and_matchup_is_consistent():
    # A long'da T olarak 3/3 kazandı; B 10 düelloda 5/5; C hep kaybediyor
    rows = [("A", "C", "T", "CT", "long")] * 3 + [("B", "C", "T", "CT", "long")] * 5 + \
           [("C", "B", "CT", "T", "long")] * 5
    model = du.strengths(_duels(rows))
    a = model["lines"][("A", "long", "T")]
    assert a["raw"] == 1.0 and 0.5 < a["strength"] < 0.9      # 3/3 = %100 değil, küçültülmüş
    assert a["conf"] == "az"
    # Güçlü T (A) zayıf CT'ye (C) karşı favori; simetri: P(A>C) + P(C>A) = 1
    p_ac = du.matchup(model, "A", "T", "C", "long")
    p_ca = du.matchup(model, "C", "CT", "A", "long")
    assert p_ac > 0.5 and abs(p_ac + p_ca - 1) < 1e-9
    assert du.matchup(model, "A", "T", "C", "yok") is None


def test_can_see_matches_known_kill_pair():
    # Görüş testi: harita geometrisi yerelde yoksa atlanır (internetten indirilmez)
    import pytest
    from awpy import data
    if not data.mesh_path("de_dust2").exists():
        pytest.skip("Dust2 geometrisi yerelde yok")
    from squadmind.analysis.geometry import can_see
    # Aynı noktadan kendine bakmak görür; site'lar arası (B ↔ A) duvarlar görüşü keser
    assert can_see("de_dust2", (1300, 1500, 0), (1300, 1500, 0))
    assert not can_see("de_dust2", (-1750, 2400, 30), (1097, 2649, 100))
