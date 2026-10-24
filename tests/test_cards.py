"""Oyuncu kartı (analysis/cards.py): ölçek, etiketler, küçültme, silah kaydı, veri yok, bağımsızlık."""

from __future__ import annotations

import pathlib

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
    kazanır; hi/lo dolgu çifti β'yı ayırt edilir kılar); "star" yalnız AWP'de çok güçlüdür."""
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
    assert "doğrusal DEĞİL" in (cd.__doc__ or "")                   # uyarı docstring'de


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


def test_internal_shift_shrinks_with_few_duels() -> None:
    # İç hesap: "star" AWP'de 400 düelloda çok kazanır; "newbie" 4 düello → küçültme büyük fark yaratır
    du, elo = _duels([("star", "x", "awp", 400), ("star", "w", "rifle", 400), ("newbie", "y", "awp", 4),
                      ("avg", "z", "awp", 400)])
    cm = cd.fit_card_model(du, elo)
    star = cm.d[("star", "awp")][0]
    newbie = cm.d.get(("newbie", "awp"), (0.0, 0, 0))[0]
    assert star > 0.05 and abs(newbie) < 0.5 * abs(star)


def test_weapon_record_raw_wilson_unvalidated() -> None:
    du, _ = _duels([("star", "x", "awp", 60), ("newbie", "y", "awp", 4)])
    counts = cd.weapon_counts(du)
    big, small = cd.weapon_record(counts, "star", "awp"), cd.weapon_record(counts, "newbie", "awp")
    assert big.n >= 60 and big.label == "yeterli" and big.lo < big.wins / big.n < big.hi
    assert small.n < cd.MIN_N and small.label == "veri yok" and small.lo is None and small.hi is None
    assert not big.validated and not small.validated
    none = cd.weapon_record(counts, "hiç_yok", "rifle")
    assert none.n == 0 and none.label == "veri yok"


def test_card_has_no_weapon_score_or_shift() -> None:
    du, _ = _duels([("star", "x", "awp", 60)])
    card = cd.player_card("star", feats=_feats(), counts=cd.weapon_counts(du), nickname="s", level=8)
    d = card.to_dict()
    for w in d["weapons"].values():
        assert set(w) == {"wins", "n", "lo", "hi", "label", "validated"} and w["validated"] is False
    assert set(d) == {"nickname", "steam_id", "faceit_level", "elo", "general", "weapons", "descriptive", "counts"}
    assert set(d["weapons"]) == set(cd.SHOWN_SKILLS)
    assert set(d["counts"]) == {"demos", "rounds", "duels"} and d["faceit_level"] == 8
    assert d["elo"] == 1700.0 and {"score", "lo", "hi", "conf"} <= set(d["general"])


def test_no_data_cases() -> None:
    card = cd.player_card("yok", feats=None)
    assert card.general.value is None and card.general.label == "veri yok" and card.elo is None
    assert all(w.n == 0 and w.label == "veri yok" for w in card.weapons.values())
    assert set(card.weapons) == set(cd.SHOWN_SKILLS)
    # boş düello tablosu da çökmez
    empty = pl.DataFrame(schema={"match_id": pl.String, "winner": pl.String, "loser": pl.String,
                                 "winner_side": pl.String, "winner_cls": pl.String,
                                 "loser_cls": pl.String, "dist": pl.Float64})
    assert cd.fit_card_model(empty, {}).n_duels == 0 and cd.weapon_counts(empty) == {}


def test_general_uses_combined_elo_with_interval() -> None:
    f = _feats(1900.0)
    card = cd.player_card("a", feats=f)
    assert card.general.value == cd.elo_to_score(f.combined_elo)
    assert card.general.lo <= card.general.value <= card.general.hi
    assert cd.player_card("b", feats=_feats(2300.0)).general.value > card.general.value


def test_matchup_unaffected_by_cards() -> None:
    """Kart hesabı rating.matchup çıktısını değiştirmez; arena_duel kart modülünü çağırmaz."""
    model = rt.fit_model(pl.DataFrame(schema={"match_id": pl.String, "winner": pl.String, "loser": pl.String,
                                              "winner_side": pl.String, "line": pl.String}),
                         lambda m, s: _feats())
    fa, fb = _feats(1900.0), _feats(1500.0)
    before = rt.matchup(model, "a", "T", "b", None, fa, fb)
    du, elo = _duels([("star", "x", "awp", 100)])
    cd.fit_card_model(du, elo)
    cd.player_card("star", feats=fa, counts=cd.weapon_counts(du)).to_dict()
    after = rt.matchup(model, "a", "T", "b", None, fa, fb)
    assert (before.p, before.lo, before.hi, before.conf) == (after.p, after.lo, after.hi, after.conf)
    app = pathlib.Path(__file__).resolve().parents[1] / "src" / "squadmind" / "web" / "app.py"
    text = app.read_text(encoding="utf-8")
    start = text.index("async def arena_duel")
    end = text.find("\n    @", start)
    body = text[start:end if end > 0 else len(text)]
    assert "cards" not in body and "analysis.cards" not in text
