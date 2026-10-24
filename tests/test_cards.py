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


def _lobby_counts() -> dict:
    """40 oyuncu, her biri tüfekte 100 düello, 50 ± 10 galibiyet (ön bilgi ölçülebilsin)."""
    return {(f"p{i}", "rifle"): (50 + (i % 11 - 5) * 2, 100) for i in range(40)}


def test_weapon_record_shrinks_to_mean_with_weight_and_detail() -> None:
    counts = {**_lobby_counts(), ("newbie", "rifle"): (10, 10), ("mid", "rifle"): (30, 30),
              ("big", "rifle"): (300, 300)}
    pri = cd.weapon_priors(counts)
    mu = pri["rifle"].mu
    small, mid, big = (cd.weapon_record(counts, s, "rifle", pri) for s in ("newbie", "mid", "big"))
    # az veride "veri yok" değil: ortalamaya çekilmiş değer + yetersiz veri + ayrıntı
    assert small.label == "yetersiz veri" and small.value is not None and small.lo < small.value < small.hi
    assert abs(small.value - mu) < 0.5 * abs(small.raw - mu) and small.raw == 1.0
    assert "10 düello" in small.detail and "kişisel veri ağırlığı" in small.detail
    assert mid.label == "az veri" and big.label == "yeterli"
    # n arttıkça kişisel ağırlık artar, değer ham orana yaklaşır
    assert small.weight < mid.weight < big.weight < 1.0
    assert abs(big.value - big.raw) < abs(small.value - small.raw)
    assert not small.validated and not big.validated
    none = cd.weapon_record(counts, "hiç_yok", "rifle", pri)
    assert none.n == 0 and none.weight == 0.0 and abs(none.value - mu) < 1e-12 and none.label == "yetersiz veri"


def test_card_contract_keys() -> None:
    du, _ = _duels([("star", "x", "awp", 60)])
    card = cd.player_card("star", feats=_feats(), counts=cd.weapon_counts(du), nickname="s", level=8)
    d = card.to_dict()
    for w in d["weapons"].values():
        assert set(w) == {"wins", "n", "lo", "hi", "label", "validated", "value", "raw", "weight", "detail"}
        assert w["validated"] is False and w["value"] is not None
    assert set(d) == {"nickname", "steam_id", "faceit_level", "elo", "general", "power", "weapons",
                      "descriptive", "counts"}
    assert set(d["weapons"]) == set(cd.SHOWN_SKILLS)
    assert set(d["counts"]) == {"demos", "rounds", "duels"} and d["faceit_level"] == 8
    assert d["elo"] == 1700.0 and {"score", "lo", "hi", "conf"} <= set(d["general"])
    assert d["power"]["validated"] is False and d["power"]["components"][0]["key"] == "elo"


def test_no_data_cases() -> None:
    card = cd.player_card("yok", feats=None)
    assert card.general.value is None and card.general.label == "veri yok" and card.elo is None
    assert card.power is None and card.to_dict()["power"] is None
    # veri hiç yoksa da silah satırı dolu: ortalama değer, ağırlık 0, "yetersiz veri"
    assert all(w.n == 0 and w.label == "yetersiz veri" and w.weight == 0.0 and w.value is not None
               for w in card.weapons.values())
    assert set(card.weapons) == set(cd.SHOWN_SKILLS)
    # boş düello tablosu da çökmez
    empty = pl.DataFrame(schema={"match_id": pl.String, "winner": pl.String, "loser": pl.String,
                                 "winner_side": pl.String, "winner_cls": pl.String,
                                 "loser_cls": pl.String, "dist": pl.Float64})
    assert cd.fit_card_model(empty, {}).n_duels == 0 and cd.weapon_counts(empty) == {}


def test_prior_shrinks_and_flat_when_no_spread() -> None:
    spread = {f"p{i}": [(50.0 + (i % 11 - 5) * 2, 100.0)] for i in range(40)}
    pr = cd.fit_prior(spread)
    assert 0.45 < pr.mu < 0.55 and pr.tau > 0.02 and not pr.flat and pr.m < cd.MAX_M
    v1, w1 = pr.shrink(10, 10)
    v2, w2 = pr.shrink(100, 100)
    assert pr.mu < v1 < v2 < 1.0 and w1 < w2           # az veri ortalamaya yakın, çok veri ham orana
    assert pr.shrink(0, 0) == (pr.mu, 0.0)
    # oyuncular arası fark gürültüden küçükse (herkes tam 50/100) bileşen "ölçülemiyor" sayılır
    same = {f"p{i}": [(50.0, 100.0)] for i in range(40)}
    assert cd.fit_prior(same).flat
    # veri < 2 oyuncu: çökmez, tam ortalama
    assert cd.fit_prior({"a": [(3.0, 10.0)]}).flat and cd.fit_prior({}).flat


def test_descriptive_shrunk_stat_never_none() -> None:
    pr = cd.fit_prior({f"p{i}": [(50.0 + (i % 11 - 5) * 2, 100.0)] for i in range(40)})
    few = cd.shrunk_stat(pr, 4, 5, "kill")
    none = cd.shrunk_stat(pr, 0, 0, "kill")
    many = cd.shrunk_stat(pr, 80, 100, "kill")
    assert few.label == "yetersiz veri" and few.value is not None and few.raw == 0.8
    assert none.value == pr.mu and none.weight == 0.0 and none.raw is None
    assert abs(few.value - pr.mu) < abs(many.value - pr.mu)
    assert 0.0 <= many.lo < many.value < many.hi <= 1.0 and "kişisel veri ağırlığı" in many.detail


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


def test_general_score_uses_live_elo_not_combined():
    """Profesör 24.10 (a): genel puan canlı ELO'dan; stat eki genel puanı değiştirmez."""
    from squadmind.analysis import cards
    from squadmind.analysis import rating as rt
    base = dict(x=None, var=(0.0, 0.0, 0.0, 0.0), elo=1885.0, elo_known=True, share=0.5, share_raw=0.5,
                adr=80.0, weight=0.5, n_matches=100, stat_adj=0.0, combined_elo=1885.0, combined_sd=0.0,
                elo_exact=True)
    fields = rt.PlayerFeatures.__dataclass_fields__
    plain = rt.PlayerFeatures(**{k: v for k, v in base.items() if k in fields})
    boosted = rt.PlayerFeatures(**{**{k: v for k, v in base.items() if k in fields},
                                   "stat_adj": 250.0, "combined_elo": 2135.0})
    a = cards.player_card("x", feats=plain).general.value
    b = cards.player_card("x", feats=boosted).general.value
    assert a == b and abs(a - cards.elo_to_score(1885.0)) < 1e-9


# --- tek güç ---------------------------------------------------------------------------------------


def _synthetic_power():
    """Sentetik düello + bileşen sayımları + stat ekleri → fit_power."""
    du, elo = _duels([("star", "x", "awp", 60), ("a", "b", "rifle", 150), ("c", "d", "rifle", 150)])
    rng = np.random.default_rng(3)
    counts, stat_of = {}, {}
    for (mid, sid) in elo:
        counts[(mid, sid)] = {"opening": (float(rng.integers(0, 7)), 6.0), "trade": (float(rng.integers(0, 4)), 8.0),
                              "clutch": (float(rng.integers(0, 2)), 2.0),
                              "support": (float(rng.normal(300, 80)), 20.0), "rounds": (20.0, 0.0)}
        stat_of[(mid, sid)] = float(rng.normal(0, 60))
    return du, counts, elo, stat_of, cd.fit_power(du, counts, elo, stat_of)


def test_power_elo_only_equals_elo() -> None:
    f = _feats(1750.0)
    p = cd.player_power(None, "yok", f)
    assert p.elo == 1750.0 and len(p.parts) == 1 and p.parts[0].key == "elo"
    assert p.score == cd.elo_to_score(1750.0) and p.lo_elo == p.hi_elo == 1750.0   # kesin ELO: aralık 0
    card = cd.player_card("yok", feats=f)           # model yoksa kart da ELO-only güç taşır
    assert card.power is not None and card.power.elo == 1750.0 and card.to_dict()["power"]["validated"] is False


def test_power_parts_sum_to_power_and_bounded() -> None:
    du, counts, elo, stat_of, pm = _synthetic_power()
    assert pm.beta > 0 and set(pm.gamma) == set(cd.POWER_COMPONENTS)
    for sid, adj in (("star", 80.0), ("a", -250.0), ("zz", 500.0), ("c", 0.0)):
        f = _feats(1700.0)
        p = cd.player_power(pm, sid, f, adj)
        assert abs(sum(x.contribution_elo for x in p.parts) - p.elo) < 1e-9
        assert abs(p.elo - 1700.0) <= rt.MAX_STAT_ADJ + 1e-9           # toplam katkı sınırlı
        assert p.lo_elo <= p.elo <= p.hi_elo and p.parts[0].contribution_elo == 1700.0
        d = p.to_dict()
        assert d["elo"] == round(p.elo, 1) and [c["key"] for c in d["components"]][:2] == ["elo", "stat"]
        assert all(set(c) == {"key", "name", "contribution_elo", "weight", "elo_per_sd", "z", "data_weight", "n",
                              "tested"} for c in d["components"])
    # veri olmayan oyuncuda (z = 0) ve stat eki yoksa güç = ELO
    p0 = cd.player_power(pm, "hiç_yok", _feats(1700.0), 0.0)
    assert abs(p0.elo - 1700.0) < 1e-9 and "ELO 1700" in p0.formula()


def test_power_unvalidated_and_crossfit_differs_from_full() -> None:
    du, counts, elo, stat_of, pm = _synthetic_power()
    assert cd.POWER_VALIDATED is False and not any(cd.POWER_TESTED.values())
    # çapraz-uydurma: bir maçın düello özellikleri o maç dışarıda hesaplanır → tüm-veri tasarımından farklı
    sd = cd.stat_scale(stat_of)
    D = cd.crossfit_design(du, counts, elo, stat_of, sd)
    full = cd.design(cd.build_state(counts, set(du["match_id"].to_list()), du, elo, sd), cd._prep(du, elo), stat_of)
    assert D.Z.shape[1] == len(cd.DESIGN_COLS) and len(D.y) == full.Z.shape[0]
    assert not np.allclose(np.sort(D.Z[:, 1]), np.sort(full.Z[:, 1]))


def test_power_component_weight_and_flat_off() -> None:
    du, counts, elo, stat_of, pm = _synthetic_power()
    w = [pm.state.shrunk["opening"].get(sid, (0, 0.0, 0))[1] for sid in ("a", "star")]
    assert all(0.0 <= v < 1.0 for v in w)
    # tüm oyuncuları aynı yapan bileşen kapanır: z = 0, katkı 0
    same = {k: {**v, "opening": (3.0, 6.0)} for k, v in counts.items()}
    st = cd.build_state(same, set(du["match_id"].to_list()), du, elo, 50.0)
    assert st.priors["opening"].flat and all(z == 0.0 for z, _, _ in st.shrunk["opening"].values())
