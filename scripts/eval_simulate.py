"""Round simülasyonunun (analysis/simulate.py) zaman bölmeli sınaması — tekrar üretilebilir.

Kullanım:  uv run python scripts/eval_simulate.py [--db <duckdb>] [--map de_dust2] [--start 6]
                                                   [--horizon 40] [--n 2000] [--cv forward|lomo]

Her round için (gerçek veri): T'lerin ilk `horizon` sn'deki gerçek rotaları (positions_snapshot,
2 sn'de bir kayıt, arası doğrusal), CT'lerin ilk `ct_hold` sn'deki gerçek rotaları (sonra TUTMA),
ana silah sınıfı (rotanın içinde en çok tutulan birincil silah), maç anındaki seviye ELO'ları
→ simülasyon → T'nin round'u kazanma olasılığı. Karşılaştırma tabanları aynı katlarda eğitilir:
yazı-tura, yalnız ekonomi (alım tipi çifti, küçültmeli), yalnız ELO ortalaması farkı.

Sızıntı notu: rota ÖLENLER için ölüm yerinde biter (o oyuncu simülasyonda orada durur). Bu, gerçek
round'dan alınan bilgidir; arenadaki kullanıcı rotayı round öncesi verir. Etkiyi ölçmek için
"düello kapalı kontrol" (tüm düellolar %50) aynı rotalarla çalışır.

Katlar: forward = maç k'yi ilk k maçla eğitilen parametrelerle tahmin et (k = start … son);
lomo = her maç, diğer tüm maçlarla eğitilen parametrelerle (daha çok test round'u).
Eğitilen şeyler: β (ELO), silah katsayıları, kazananın hasar dağılımı, son durum tabloları,
ekonomi ve ELO tabanları. Sabit: HP katsayısı 0,717 (rating.py; tüm veride bulundu, hafif sızıntı).
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass, replace

import duckdb
import numpy as np
import polars as pl

from squadmind.analysis import duels as du
from squadmind.analysis import rating as rt
from squadmind.analysis import simulate as sim
from squadmind.analysis.player_metrics import PRIMARY_CATEGORIES, DemoData, _weapon_category, load_demo_data
from squadmind.config import DB_PATH
from squadmind.faceit.client import FaceitClient

TICK = 64.0
SITE_PLACES = {"BombsiteA": "A", "BombsiteB": "B"}   # plant alanı sayılan bölge adları (Dust2)


@dataclass
class Case:
    """Bir round: simülasyon girdisi + gerçek sonuç + sınama bilgisi."""

    mi: int                       # maç sırası
    match_id: str
    round_no: int
    y: float                      # T kazandı mı
    t_buy: str
    ct_buy: str
    t_units: list[sim.Unit]
    ct_units: list[sim.Unit]
    elo_diff: float               # T ortalama ELO − CT ortalama ELO
    t_alive: dict[float, int]     # zaman → hayatta T (gerçek), eğitim tabloları için
    ct_alive: dict[float, int]
    plant_time: float | None
    plant_t_alive: int | None
    plant_ct_alive: int | None
    t_equip: float = 0.0          # takım ekipman değeri (rounds tablosu)
    ct_equip: float = 0.0
    first_kill: tuple[float, float, float] | None = None   # gerçek ilk kill: (an, x, y) orta nokta


def load_levels(order: list[str], client: FaceitClient) -> dict[tuple[str, str], int | None]:
    """(maç, steam_id) → maç anındaki FACEIT seviyesi (önbellekli maç detayı)."""
    out: dict[tuple[str, str], int | None] = {}
    for mid in order:
        det = client.get_match_details(mid)
        for team in (det.get("teams") or {}).values():
            for p in team.get("roster") or []:
                out[(mid, p.get("game_player_id"))] = p.get("game_skill_level")
    return out


def damage_taken(d: DemoData, duel: pl.DataFrame) -> list[float]:
    """Her silahlı düelloda kazananın, KAZANMADAN ÖNCE kaybedenden yediği hasar (kırpılmış)."""
    k = d.kills.filter((pl.col("attacker_side") != pl.col("victim_side"))
                       & pl.col("weapon").is_in(du._GUN_WEAPONS)
                       & pl.col("attacker_x").is_not_null() & pl.col("victim_x").is_not_null()
                       ).select("match_id", pl.col("round_no").cast(pl.Int64), "tick",
                                pl.col("attacker").alias("w"), pl.col("victim").alias("l"))
    dm = (d.damages.select("match_id", pl.col("round_no").cast(pl.Int64), pl.col("tick").alias("dtick"),
                           pl.col("attacker").alias("l"), pl.col("victim").alias("w"), "dmg"))
    j = (k.join(dm, on=["match_id", "round_no", "l", "w"], how="left")
         .filter(pl.col("dtick").is_null() | (pl.col("dtick") < pl.col("tick")))
         .group_by("match_id", "round_no", "tick", "w", "l").agg(pl.col("dmg").fill_null(0).sum().alias("taken")))
    return j


def first_kill(kg: pl.DataFrame | None) -> tuple[float, float, float] | None:
    """Round'un ilk rakip kill'i: (an, saldıran-kurban orta noktası x, y); konum yoksa None."""
    if kg is None:
        return None
    k = kg.filter((pl.col("attacker_side") != pl.col("victim_side")) & pl.col("attacker_x").is_not_null()
                  & pl.col("victim_x").is_not_null()).sort("time_in_round")
    if k.height == 0:
        return None
    r = k.row(0, named=True)
    return (r["time_in_round"], (r["attacker_x"] + r["victim_x"]) / 2, (r["attacker_y"] + r["victim_y"]) / 2)


def build_cases(d: DemoData, map_name: str, levels: dict, order: list[str], horizon: float,
                ct_hold: float, ct_dynamic: bool) -> list[Case]:
    """Her round için simülasyon girdisi. ELO: seviye ortası (bilinmeyen = bilinenlerin ortalaması)."""
    mi_of = {m: i for i, m in enumerate(order)}
    known = [rt.level_elo(v)[0] for v in levels.values() if v and rt.level_elo(v)[0] is not None]
    mean_elo = float(np.mean(known))

    def elo(mid: str, sid: str) -> float:
        e = rt.level_elo(levels.get((mid, sid)))[0]
        return mean_elo if e is None else e

    pos = d.positions.with_columns(pl.col("round_no").cast(pl.Int64),
                                   _weapon_category("active_weapon").alias("wcat"))
    groups = pos.partition_by(["match_id", "round_no", "player"], as_dict=True, maintain_order=False)
    by_round: dict[tuple[str, int], list[tuple[str, pl.DataFrame]]] = {}
    for (mid, rn, pl_), g in groups.items():
        by_round.setdefault((mid, rn), []).append((pl_, g.sort("time_in_round")))
    kills = d.kills.with_columns(pl.col("round_no").cast(pl.Int64))
    kby = {k: g for k, g in kills.partition_by(["match_id", "round_no"], as_dict=True).items()}
    rounds = d.rounds.with_columns(pl.col("round_no").cast(pl.Int64))
    cases: list[Case] = []
    for r in rounds.iter_rows(named=True):
        mid, rn = r["match_id"], r["round_no"]
        if mid not in mi_of or (mid, rn) not in by_round or r["winner_side"] not in ("T", "CT"):
            continue
        t_units, ct_units = [], []
        for sid, g in by_round[(mid, rn)]:
            side = g["side"][0]
            hz = horizon if (side == "T" or ct_dynamic) else ct_hold
            g = g.filter(pl.col("time_in_round") <= hz)
            if g.height == 0:
                continue
            prim = g.filter(pl.col("wcat").is_in(PRIMARY_CATEGORIES))
            cls = prim["wcat"].mode().sort().first() if prim.height else "pistol"
            t = g["time_in_round"].to_list()
            xyz = list(zip(g["x"].to_list(), g["y"].to_list(), g["z"].to_list(), strict=True))
            plant = False
            if side == "T":
                # Planter: hayatta (rota ufka yakın bitiyor) ve rotanın SONU site plant alanında
                places = g["place_name"].to_list()
                if t[-1] >= horizon - 2.1 and places[-1] in SITE_PLACES:
                    i = len(places) - 1
                    while i > 0 and places[i - 1] == places[-1]:
                        i -= 1
                    t, xyz, plant = t[:i + 1], xyz[:i + 1], True    # varış anında biter (sonra kurma)
            unit = sim.Unit(sid, elo(mid, sid), sim.Track(t, xyz), cls, plant)
            (t_units if side == "T" else ct_units).append(unit)
        if not t_units or not ct_units:
            continue
        kg = kby.get((mid, rn))
        ktimes = [] if kg is None else list(zip(kg["time_in_round"].to_list(), kg["victim_side"].to_list(),
                                                 strict=True))
        plant_time = None if r["plant_tick"] is None else (r["plant_tick"] - r["freeze_end_tick"]) / TICK

        def alive(at: float) -> tuple[int, int]:
            dead_t = sum(1 for tt, s in ktimes if tt <= at and s == "T")
            dead_c = sum(1 for tt, s in ktimes if tt <= at and s == "CT")
            return len(t_units) - dead_t, len(ct_units) - dead_c

        pa = alive(plant_time) if plant_time is not None else (None, None)
        at_h = alive(horizon)
        cases.append(Case(
            mi_of[mid], mid, rn, 1.0 if r["winner_side"] == "T" else 0.0, r["t_buy_type"], r["ct_buy_type"],
            t_units, ct_units,
            float(np.mean([u.elo for u in t_units]) - np.mean([u.elo for u in ct_units])),
            {horizon: at_h[0]}, {horizon: at_h[1]}, plant_time, pa[0], pa[1],
            float(r["t_equipment"] or 0), float(r["ct_equipment"] or 0), first_kill(kg)))
    return cases


def duel_table(d: DemoData, duel: pl.DataFrame, levels: dict, order: list[str]) -> pl.DataFrame:
    """Düello başına: T lehine sonuç, ELO farkı, silah sınıfları, mesafe, kazananın yediği hasar."""
    mi_of = {m: i for i, m in enumerate(order)}
    known = [rt.level_elo(v)[0] for v in levels.values() if v and rt.level_elo(v)[0] is not None]
    mean_elo = float(np.mean(known))

    def elo(mid: str, sid: str) -> float:
        e = rt.level_elo(levels.get((mid, sid)))[0]
        return mean_elo if e is None else e

    rows = []
    for r in duel.iter_rows(named=True):
        tw = r["winner_side"] == "T"
        t, c = (r["winner"], r["loser"]) if tw else (r["loser"], r["winner"])
        rows.append(dict(mi=mi_of[r["match_id"]], match_id=r["match_id"], round_no=r["round_no"],
                         winner=r["winner"], loser=r["loser"], y=1.0 if tw else 0.0,
                         d_elo=elo(r["match_id"], t) - elo(r["match_id"], c),
                         cls_t=r["winner_cls"] if tw else r["loser_cls"],
                         cls_ct=r["loser_cls"] if tw else r["winner_cls"], dist=r["dist"]))
    return pl.DataFrame(rows)


def fit_fold(dt: pl.DataFrame, taken: pl.DataFrame, cases: list[Case], horizon: float) -> sim.DuelParams:
    """Eğitim düellolarından + round'larından simülasyon parametreleri."""
    X = (dt["d_elo"].to_numpy() / rt.ELO_UNIT).reshape(-1, 1)
    y = dt["y"].to_numpy()
    beta, cov = rt._fit_logistic(X, np.zeros(len(y)), y, 1.0)
    we = rt.fit_weapon_effect(dt["cls_t"].to_list(), dt["cls_ct"].to_list(), dt["dist"].to_list(),
                              y, (X[:, 0] * beta[0]).tolist(), simple=True)
    # Kazananın yediği hasar: eğitim düellolarının (maç, round, kazanan, kaybeden) eşleşmesi
    tk = taken.join(dt.select("match_id", "round_no", pl.col("winner").alias("w"), pl.col("loser").alias("l")),
                    on=["match_id", "round_no", "w", "l"], how="inner")
    dmg = np.quantile(tk["taken"].to_numpy(), np.linspace(0, 1, 101)) if tk.height else np.zeros(1)
    # Son durum tabloları (gerçek hayatta kalan farkı → round sonucu)
    u_d, u_y, p_d, p_y = [], [], [], []
    for c in cases:
        if c.plant_time is not None and c.plant_t_alive is not None:
            p_d.append(c.plant_t_alive - c.plant_ct_alive); p_y.append(c.y)
        if (c.plant_time is None or c.plant_time > horizon) and c.t_alive[horizon] > 0 and c.ct_alive[horizon] > 0:
            u_d.append(c.t_alive[horizon] - c.ct_alive[horizon]); u_y.append(c.y)
    return sim.DuelParams(
        beta=float(beta[0]), beta_se=float(math.sqrt(cov[0, 0])),
        weapon=tuple(float(v) for v in we.beta),
        weapon_se=tuple(float(math.sqrt(max(cov_ii, 0.0))) for cov_ii in np.diag(we.cov)),
        damage=tuple(float(v) for v in dmg),
        end_unplanted=sim.fit_end_table(u_d, u_y), end_planted=sim.fit_end_table(p_d, p_y))


# --- Karşılaştırma tabanları ----------------------------------------------------------------------


def economy_table(train: list[Case], k: float = 10.0) -> dict:
    """(T alımı, CT alımı) → T kazanma oranı, genel orana k pseudo-gözlemle küçültülmüş."""
    overall = (sum(c.y for c in train) + 1) / (len(train) + 2)
    cell: dict[tuple[str, str], list[float]] = {}
    for c in train:
        cell.setdefault((c.t_buy, c.ct_buy), []).append(c.y)
    return {"all": overall, **{key: (sum(v) + k * overall) / (len(v) + k) for key, v in cell.items()}}


def elo_logit(train: list[Case]) -> tuple[float, float]:
    """logit P(T) = a + b · (ELO ort. farkı / 100); hafif ridge."""
    X = np.column_stack([np.ones(len(train)), [c.elo_diff / 100 for c in train]])
    b, _ = rt._fit_logistic(X, np.zeros(len(train)), np.array([c.y for c in train]), np.array([1e-6, 1.0]))
    return float(b[0]), float(b[1])


def _feat_equip(c: Case) -> list[float]:
    return [(c.t_equip - c.ct_equip) / 1000.0]


def _feat_weapons(c: Case) -> list[float]:
    """T − CT silah sınıfı sayıları (tabanca, SMG, AWP, Scout, ağır): sim'siz silah tabanı."""
    cnt = lambda us, k: sum(1 for u in us if u.cls == k)   # noqa: E731
    return [cnt(c.t_units, k) - cnt(c.ct_units, k) for k in ("pistol", "smg", "awp", "scout", "heavy")]


def logit_base(train: list[Case], feat) -> tuple[np.ndarray, callable]:
    """Kesişim + özellikler üzerinde hafif ridge lojistik; döner (katsayılar, tahmin işlevi)."""
    X = np.array([[1.0, *feat(c)] for c in train])
    pen = np.array([1e-6] + [1.0] * (X.shape[1] - 1))
    b, _ = rt._fit_logistic(X, np.zeros(len(train)), np.array([c.y for c in train]), pen)
    return b, lambda c: 1 / (1 + math.exp(-float(np.dot(b, [1.0, *feat(c)]))))


def clip(p: float) -> float:
    return min(max(p, 0.02), 0.98)


def slope(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Kalibrasyon: y ~ a + b·logit(p) (b = 1, a = 0 ideal). Döner (b, a)."""
    z = np.log(p / (1 - p))
    X = np.column_stack([np.ones(len(z)), z])
    b, _ = rt._fit_logistic(X, np.zeros(len(y)), y, np.array([1e-6, 1e-6]))
    return float(b[1]), float(b[0])


def boot_diff(loss_a: np.ndarray, loss_b: np.ndarray, groups: np.ndarray, reps: int = 4000, seed: int = 7
              ) -> tuple[float, float, float]:
    """Ortalama (loss_a − loss_b) + maç-kümeli bootstrap %90 aralığı. Pozitif = b daha iyi."""
    rng = np.random.default_rng(seed)
    ids = np.unique(groups)
    idx = {g: np.nonzero(groups == g)[0] for g in ids}
    diff = loss_a - loss_b
    out = []
    for _ in range(reps):
        pick = rng.choice(ids, size=len(ids), replace=True)
        sel = np.concatenate([idx[g] for g in pick])
        out.append(diff[sel].mean())
    return float(diff.mean()), float(np.percentile(out, 5)), float(np.percentile(out, 95))


def ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Round başına log-kayıp."""
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


# --- Ana akış -------------------------------------------------------------------------------------


# Bileşen çıkarma (ablation): hangi düello terimleri açık kalsın
ABLATE = {"main": {"elo", "weapon", "hp"}, "ctdyn": {"elo", "weapon", "hp"}, "control": set(),
          "elo": {"elo"}, "weapon": {"weapon"}, "hp": {"hp"}, "noelo": {"weapon", "hp"}}


def ablated(params: sim.DuelParams, keep: set[str]) -> sim.DuelParams:
    """Sadece `keep` terimleri açık bırakır (kapalı terim: katsayı 0, belirsizlik 0)."""
    return replace(
        params,
        beta=params.beta if "elo" in keep else 0.0, beta_se=params.beta_se if "elo" in keep else 0.0,
        weapon=params.weapon if "weapon" in keep else (0.0, 0.0, 0.0),
        weapon_se=params.weapon_se if "weapon" in keep else (0.0, 0.0, 0.0),
        hp_coef=params.hp_coef if "hp" in keep else 0.0, hp_se=params.hp_se if "hp" in keep else 0.0)


def predict_all(cases: list[Case], dt: pl.DataFrame, taken: pl.DataFrame, folds: list[tuple[list[int], list[int]]],
                horizon: float, n: int, preps: dict, los, keep: set[str]) -> dict[int, sim.SimResult]:
    """Her kat için parametreleri eğit, test round'larını simüle et. Döner: case sırası → SimResult."""
    out: dict[int, sim.SimResult] = {}
    for train_mi, test_mi in folds:
        tr = [c for c in cases if c.mi in train_mi]
        params = ablated(fit_fold(dt.filter(pl.col("mi").is_in(train_mi)), taken, tr, horizon), keep)
        for ci, c in enumerate(cases):
            if c.mi not in test_mi:
                continue
            if ci not in preps:
                preps[ci] = sim.prepare(sim.Scenario("de_dust2", tuple(c.t_units), tuple(c.ct_units),
                                                     horizon=horizon), los)
            out[ci] = sim.run(preps[ci], params, n=n, seed=1000 + ci, uncertainty=bool(keep))
    return out


def report(name: str, cases: list[Case], preds: dict[int, sim.SimResult], base: dict[str, dict[int, float]],
           show_cal: bool = True) -> dict:
    ids = sorted(preds)
    y = np.array([cases[i].y for i in ids])
    g = np.array([cases[i].mi for i in ids])
    p = np.array([clip(preds[i].p_t) for i in ids])
    l_sim = ll(p, y)
    print(f"\n== {name}: {len(ids)} round, {len(set(g))} maç; T kazanma oranı {y.mean():.3f}, ortalama tahmin {p.mean():.3f}")
    print(f"  {'model':28s} log-kayıp  Brier   isabet | tabana göre kazanç (+ = sim daha iyi), %90")
    rows = {}
    for bn, bp in [("yazı-tura", None), *base.items()]:
        pb = np.full(len(ids), 0.5) if bp is None else np.array([clip(bp[i]) for i in ids])
        lb = ll(pb, y)
        d, lo, hi = boot_diff(lb, l_sim, g)
        rows[bn] = (lb.mean(), d, lo, hi)
        print(f"  {bn:28s} {lb.mean():.4f}   {np.mean((pb - y) ** 2):.4f}  {np.mean((pb > .5) == (y == 1)):.3f}"
              f"  | {d:+.4f} [{lo:+.4f}, {hi:+.4f}]")
    print(f"  {'sim':28s} {l_sim.mean():.4f}   {np.mean((p - y) ** 2):.4f}  {np.mean((p > .5) == (y == 1)):.3f}")
    if show_cal:
        sl, ic = slope(p, y)
        print(f"  kalibrasyon eğimi {sl:.2f} (kesişim {ic:+.2f}); dilimler:", "; ".join(
            f"{c['pred']:.2f}->{c['obs']:.2f} (n={c['n']})" for c in rt.calibration(p, y, 5)))
    return {"loss": l_sim.mean(), "rows": rows}


def mechanism_table(cases: list[Case], preds: dict[int, sim.SimResult], horizon: float, map_name: str) -> None:
    """Mekanizma kontrolü: round nasıl bitiyor, ilk düello ne zaman / nerede (simüle vs gerçek)."""
    from collections import Counter
    ids = sorted(preds)
    print(f"\nMekanizma ({len(ids)} round, ufuk {horizon:.0f} sn): simüle vs gerçek")
    real = Counter()
    for i in ids:
        c = cases[i]
        if c.ct_alive[horizon] == 0:
            real["ct_elendi"] += 1
        elif c.t_alive[horizon] == 0 and (c.plant_time is None or c.plant_time > horizon):
            real["t_elendi"] += 1
        elif c.plant_time is not None and c.plant_time <= horizon:
            real["plant"] += 1
        else:
            real["ufuk"] += 1
    for k in sim._ENDS:
        print(f"  {k:10s} simüle {np.mean([preds[i].outcomes[k] for i in ids]):.3f}   gerçek {real[k] / len(ids):.3f}")
    fk = [cases[i].first_kill for i in ids if cases[i].first_kill is not None]
    print(f"  ilk düello: simüle oran {np.mean([preds[i].first_duel_rate for i in ids]):.3f} "
          f"gerçek {len(fk) / len(ids):.3f}; an (ort.) simüle "
          f"{np.nanmean([preds[i].first_duel_time for i in ids]):.1f} sn, gerçek ilk kill medyan "
          f"{np.median([f[0] for f in fk]):.1f} sn (ort. {np.mean([f[0] for f in fk]):.1f})")
    simp, realp = Counter(), Counter()
    for i in ids:
        for k, v in preds[i].first_duel_places.items():
            simp[k] += v
        if cases[i].first_kill is not None:
            realp[sim.nearest_place(map_name, cases[i].first_kill[1], cases[i].first_kill[2])] += 1
    sn, rn = sum(simp.values()), sum(realp.values())
    overlap = sum(min(simp[k] / sn, realp[k] / rn) for k in set(simp) | set(realp))
    print(f"  ilk düello yeri: dağılım örtüşmesi {overlap:.2f} (1 = aynı); en sık simüle / gerçek:")
    for k in sorted(set(simp) | set(realp), key=lambda k: -(simp[k] / sn + realp[k] / rn))[:8]:
        print(f"    {k:14s} {simp[k] / sn:.3f}  {realp[k] / rn:.3f}")


def pattern_table(d: DemoData, res: tuple) -> None:
    """T desenlerine göre gözlenen T kazanma oranı ve çapraz-sınanmış simülasyon ortalaması."""
    from squadmind.analysis import team_tactics as tt
    cases, preds, _ = res
    pat = {}
    seen = set()
    for r in d.roster.iter_rows(named=True):
        key = (r["match_id"], r["faction"])
        if key in seen:
            continue
        seen.add(key)
        for row in tt.t_round_patterns(d, r["steam_id"]).iter_rows(named=True):
            pat[(row["match_id"], int(row["round_no"]))] = (row["pattern"], row["target_site"])
    acc: dict[str, list[tuple[float, float]]] = {}
    for ci, r in preds.items():
        c = cases[ci]
        p = pat.get((c.match_id, c.round_no))
        if p is None:
            continue
        name = "girilmedi" if p[0] == "girilmedi" else f"{p[1]} {p[0]}"
        acc.setdefault(name, []).append((c.y, r.p_t))
    print("\nDesen bazında (T round'ları): gözlenen T kazanma vs simüle")
    print(f"  {'desen':16s} {'n':>4s} {'gözlenen':>9s} {'simüle':>8s}")
    for name, v in sorted(acc.items(), key=lambda kv: -len(kv[1])):
        arr = np.array(v)
        print(f"  {name:16s} {len(v):4d} {arr[:, 0].mean():9.3f} {arr[:, 1].mean():8.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--map", default="de_dust2")
    ap.add_argument("--start", type=int, default=6)
    ap.add_argument("--horizon", type=float, default=40.0)
    ap.add_argument("--ct-hold", type=float, default=25.0)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--cv", choices=("forward", "lomo"), default="forward")
    ap.add_argument("--variants", default="main,control,ctdyn", help="virgülle: " + ", ".join(ABLATE))
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    d = load_demo_data(con, args.map)
    order = d.match_maps.sort("date")["match_id"].to_list()
    with FaceitClient() as client:
        levels = load_levels(order, client)
    duel = du.duels(d, args.map)
    dt = duel_table(d, duel, levels, order)
    taken = damage_taken(d, duel)
    H = args.horizon
    if args.cv == "forward":
        folds = [(list(range(k)), [k]) for k in range(args.start, len(order))]
    else:
        folds = [([m for m in range(len(order)) if m != k], [k]) for k in range(len(order))]
    los = sim.geometry_los(args.map)
    results = {}
    preps_of: dict[str, dict] = {}    # görüş tabloları varyantlar arasında paylaşılır (aynı rotalar)
    for var in args.variants.split(","):
        cases = build_cases(d, args.map, levels, order, H, args.ct_hold, ct_dynamic=(var == "ctdyn"))
        t0 = time.time()
        preds = predict_all(cases, dt, taken, folds, H, args.n, preps_of.setdefault(var if var == "ctdyn" else "std", {}), los, ABLATE[var])
        print(f"\n[{var}] {len(preds)} round simüle edildi, {time.time() - t0:.0f} sn")
        eco, eld, eqd, wpd = {}, {}, {}, {}
        for train_mi, test_mi in folds:      # tabanlar aynı katlarda eğitilir
            tr = [c for c in cases if c.mi in train_mi]
            tab, (a, b) = economy_table(tr), elo_logit(tr)
            for ci, c in enumerate(cases):
                if c.mi in test_mi:
                    eco[ci] = tab.get((c.t_buy, c.ct_buy), tab["all"])
                    eld[ci] = 1 / (1 + math.exp(-(a + b * c.elo_diff / 100)))
            _, f_eq = logit_base(tr, _feat_equip)
            _, f_wp = logit_base(tr, _feat_weapons)
            for ci, c in enumerate(cases):
                if c.mi in test_mi:
                    eqd[ci], wpd[ci] = f_eq(c), f_wp(c)
        base = {"yalnız ekonomi (alım tipi)": eco, "yalnız ELO farkı": eld, "ekipman değeri farkı": eqd,
                "silah sayıları (sim'siz)": wpd}
        results[var] = (cases, preds, base)
        report(f"{var} (ufuk {H:.0f} sn, CT tutma {args.ct_hold:.0f} sn, {args.cv})", cases, preds, base)
    if "main" in results and "control" in results:    # düello modelinin geometri üstüne katkısı
        cm, pm, _ = results["main"]
        _, pc, _ = results["control"]
        ids = sorted(set(pm) & set(pc))
        y = np.array([cm[i].y for i in ids])
        g = np.array([cm[i].mi for i in ids])
        lm = ll(np.array([clip(pm[i].p_t) for i in ids]), y)
        lc = ll(np.array([clip(pc[i].p_t) for i in ids]), y)
        dd, lo, hi = boot_diff(lc, lm, g)
        print(f"\nDüello modeli (ELO + silah + can) katkısı, kontrole göre: {dd:+.4f} [{lo:+.4f}, {hi:+.4f}]")
    if "main" in results:
        mechanism_table(results["main"][0], results["main"][1], H, args.map)
        pattern_table(d, results["main"])
        cases, preds, _ = results["main"]    # süre: her senaryoda yeni (soğuk) görüş önbelleği
        params = fit_fold(dt, taken, cases, H)
        ts = []
        for ci in sorted(preds)[:: max(1, len(preds) // 40)]:
            c = cases[ci]
            t0 = time.perf_counter()
            sim.simulate(sim.Scenario(args.map, tuple(c.t_units), tuple(c.ct_units), horizon=H), params, n=2000)
            ts.append(time.perf_counter() - t0)
        print(f"\nSüre (2000 tekrar, soğuk görüş önbelleği, {len(ts)} round): medyan {np.median(ts):.2f} sn, "
              f"p90 {np.percentile(ts, 90):.2f} sn, en yavaş {max(ts):.2f} sn")


if __name__ == "__main__":
    main()
