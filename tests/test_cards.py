"""Oyuncu kartı (analysis/cards.py): ölçek, etiketler, küçültme, veri yok durumu."""

from __future__ import annotations

import numpy as np
import polars as pl

from squadmind.analysis import cards as cd
from squadmind.analysis import rating as rt


def _pop() -> rt.Population:
    hists = [(rt.History((0.5,) * 10, (70.0,) * 10, 500, 500), e) for e in (900.0, 1300.0, 1700.0, 2100.0, 1500.0, 1100.0)]
    return rt.fit_population(hists)


def _feats(elo: float = 1700.0) -> rt.PlayerFeatures:
    return rt.player_features(rt.History((0.5,) * 30, (70.0,) * 30, 1500, 1500), _pop(), elo=elo)


def _duels(spec: list[tuple[str, str, str, int]], seed: int = 1) -> tuple[pl.DataFrame, dict]:
    """spec: (A, B, A'nın silahı, düello sayısı). ELO farkı sonucu belirler (yüksek ELO daha çok
    kazanır; hi/lo dolgu çifti β'yı ayırt edilir kılar); "star" yalnız AWP'de çok güçlüdür (tüfekte sıradan: kayma genele göre)."""
    rows = []
    elo = {}
    rng = np.random.default_rng(seed)
    spec = [*spec, ("hi", "lo", "rifle", 400)]
    levels = {"hi": 2000.0, "lo": 1000.0}
    for i, (a, b, cls, n) in enumerate(spec):
        for j in range(n):
            mid = f"m{(i * 7 + j) % 6}"
            ea, eb = levels.get(a, 1500.0), levels.get(b, 1500.0)
            z = 0.4 * (ea - eb) / 100 + (1.5 if (a == "star" and cls == "awp") else 0.0)
            a_wins = rng.random() < 1 / (1 + np.exp(-z))
            rows.append({"match_id": mid, "winner": a if a_wins else b, "loser": b if a_wins else a,
                         "winner_side": "T", "winner_cls": cls if a_wins else "rifle",
                         "loser_cls": "rifle" if a_wins else cls, "dist": 800.0})
            elo[(mid, a)], elo[(mid, b)] = ea, eb
    return pl.DataFrame(rows), elo


def test_scale_monotone_and_anchored() -> None:
    xs = np.linspace(-500, 4000, 400)
    ys = [cd.elo_to_score(x) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:], strict=False))     # tek yönlü
    assert cd.elo_to_score(100) == 0 and cd.elo_to_score(50) == 0
    assert cd.elo_to_score(2401) == 100 and cd.elo_to_score(3500) == 100
    assert abs(cd.elo_to_score(1751) - 80) < 1e-9                   # seviye 9 başı
    assert abs(cd.elo_to_score(2001) - 90) < 1e-9                   # seviye 10 başı
    assert 80 < cd.elo_to_score(1800) < 90


def test_label_thresholds() -> None:
    assert cd.label_of(0) == cd.label_of(19) == "veri yok"
    assert cd.label_of(20) == cd.label_of(49) == "az veri"
    assert cd.label_of(50) == "yeterli"


def test_rate_stat_threshold_and_interval() -> None:
    assert cd.rate_stat(5, 10).value is None and cd.rate_stat(5, 10).label == "veri yok"
    s = cd.rate_stat(15, 30)
    assert s.value == 0.5 and s.label == "az veri" and s.lo < 0.5 < s.hi
    wide, narrow = cd.rate_stat(10, 20), cd.rate_stat(100, 200)
    assert (wide.hi - wide.lo) > (narrow.hi - narrow.lo)


def test_shrinkage_few_duels_stay_near_general() -> None:
    # "star" AWP'de 400 düelloda çok kazanır; "newbie" 4 düello: küçültme farkı büyük olmalı
    du, elo = _duels([("star", "x", "awp", 400), ("star", "w", "rifle", 400), ("newbie", "y", "awp", 4), ("avg", "z", "awp", 400)])
    cm = cd.fit_card_model(du, elo)
    star, newbie = cm.d[("star", "awp")][0], cm.d.get(("newbie", "awp"), (0.0, 0, 0))[0]
    assert star > 0.05 and abs(newbie) < 0.5 * abs(star)
    # az veride ELO kayması genele yakın ve "veri yok"
    f = _feats()
    sk = cd.skill_of(cm, "newbie", "awp", f)
    assert sk.score.value is None and sk.score.label == "veri yok"
    big = cd.skill_of(cm, "star", "awp", f)
    assert big.score.value is not None and big.score.label == "yeterli"
    # kayma sınırlı: ±300 ELO
    assert abs(big.shift_elo) <= cd.MAX_SKILL_SHIFT
    # aynı silah: daha çok veri, daha dar aralık (belirsizlik azalır)
    assert big.score.lo is not None and big.score.hi is not None
    assert big.score.hi - big.score.lo < 60


def test_shift_follows_direction_and_score_monotone() -> None:
    du, elo = _duels([("star", "x", "awp", 300), ("star", "w", "rifle", 300), ("avg", "z", "awp", 300)])
    cm = cd.fit_card_model(du, elo)
    f = _feats()
    s_star, s_avg = cd.skill_of(cm, "star", "awp", f), cd.skill_of(cm, "avg", "awp", f)
    assert s_star.shift_elo > s_avg.shift_elo
    assert s_star.score.value >= s_avg.score.value                    # tek yönlü eşleme


def test_no_data_cases() -> None:
    card = cd.player_card("yok", feats=None, card_model=None)
    assert card.general.value is None and card.general.label == "veri yok"
    assert all(s.score.value is None and s.n == 0 for s in card.skills.values())
    assert set(card.skills) == set(cd.SHOWN_SKILLS)
    # boş düello tablosu da çökmez
    empty = pl.DataFrame(schema={"match_id": pl.String, "winner": pl.String, "loser": pl.String,
                                 "winner_side": pl.String, "winner_cls": pl.String,
                                 "loser_cls": pl.String, "dist": pl.Float64})
    cm = cd.fit_card_model(empty, {})
    assert cm.n_duels == 0 and cd.skill_of(cm, "a", "rifle", _feats()).score.value is None


def test_general_uses_combined_elo_with_interval() -> None:
    f = _feats(1900.0)
    card = cd.player_card("a", feats=f)
    assert card.general.value == cd.elo_to_score(f.combined_elo)
    assert card.general.lo <= card.general.value <= card.general.hi
    hi_card = cd.player_card("b", feats=_feats(2300.0))
    assert hi_card.general.value > card.general.value                  # daha yüksek ELO → daha yüksek puan
