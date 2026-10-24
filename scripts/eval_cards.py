"""Silah yeteneği kaymasının (oyuncu × silah sınıfı) düello tahminine katkısı — sınama.

Kullanım:  python scripts/eval_cards.py [--db <duckdb>] [--map de_dust2] [--tau-d 0.2]
Taban: β·ΔELO (maç anındaki seviye ortası) + onaylı sade silah düzeltmesi (can yok: düello
tablosunda can verisi yok). Adaylar: + genel oyuncu artığı g; + g + silah kayması d; + d.
Her katta her şey yalnız EĞİTİM düellolarından öğrenilir. İki bölme:
  ileri   : ilk k maçla eğit, (k+1). maçı test et (k = 6 … son-1)
  bir-maç-dışarıda: her maç sırayla dışarıda, kalan hepsiyle eğit
Fark = taban log-kaybı − aday log-kaybı (pozitif = aday iyi); %90 aralık maç-kümeli bootstrap.
Kabul: g+d'nin g'ye göre (asıl "silah kayması" sınaması) ve tabana göre alt sınırı > 0 iki bölmede,
kalibrasyon eğimi 0,9-1,1. Ayrıca yalnız çekirdek kadronun düelloları.
"""

from __future__ import annotations

import argparse

import duckdb
import numpy as np
import polars as pl

from squadmind.analysis import cards as cd
from squadmind.analysis import duels as du
from squadmind.analysis import rating as rt
from squadmind.analysis.player_metrics import load_demo_data
from squadmind.config import DB_PATH
from squadmind.faceit.client import FaceitClient

CORE = ("Nocleva", "DeloneCS", "_squ1rtle_", "nicorett3")
RNG = np.random.default_rng(7)


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def boot(diff: np.ndarray, mids: np.ndarray, sel: np.ndarray | None = None, b: int = 2000
         ) -> tuple[float, float, float]:
    """Ortalama fark ve maç-kümeli %90 aralık (`sel`: alt küme maskesi)."""
    if sel is not None:
        diff, mids = diff[sel], mids[sel]
    ids = np.unique(mids)
    s = np.array([diff[mids == m].sum() for m in ids])
    n = np.array([(mids == m).sum() for m in ids])
    out = []
    for _ in range(b):
        k = RNG.integers(0, len(ids), len(ids))
        out.append(s[k].sum() / n[k].sum())
    lo, hi = np.percentile(out, [5, 95])
    return float(diff.mean()), float(lo), float(hi)


def slope(logit_p: np.ndarray, y: np.ndarray, off: np.ndarray | None = None) -> float:
    """Kalibrasyon eğimi: y ~ off + s·x (lojistik); s = 1 iyi kalibrasyon."""
    x = logit_p.reshape(-1, 1)
    beta, _ = rt._fit_logistic(x, np.zeros(len(y)) if off is None else off, y, 1e-6)
    return float(beta[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--map", default="de_dust2")
    ap.add_argument("--start", type=int, default=6)
    ap.add_argument("--tau-d", type=float, default=cd.TAU_D)
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    d = load_demo_data(con, args.map)
    duel = du.duels(d, args.map)
    order = d.match_maps.sort("date")["match_id"].to_list()
    duel = duel.with_columns(pl.col("match_id").replace_strict({m: i for i, m in enumerate(order)}).alias("mi"))
    core = set(d.roster.filter(pl.col("nickname").is_in(CORE))["steam_id"].to_list())

    elo_of: dict[tuple[str, str], float] = {}
    with FaceitClient() as client:
        for mid in order:
            det = client.get_match_details(mid)
            for team in det["teams"].values():
                for p in team["roster"]:
                    e = rt.level_elo(p.get("game_skill_level"))[0]
                    if e is not None:
                        elo_of[(mid, p["game_player_id"])] = e

    names = ["taban", "taban+g", "taban+g+d", "taban+d"]
    P: dict[str, list[float]] = {n: [] for n in names}
    ys, mids, is_core, fold = [], [], [], []
    split_of = {"ileri": [(k, duel.filter(pl.col("mi") < k)) for k in range(args.start, len(order))],
                "bir-maç-dışarıda": [(k, duel.filter(pl.col("mi") != k)) for k in range(len(order))]}
    results: dict[str, dict] = {}
    for split, folds in split_of.items():
        for n in names:
            P[n] = []
        ys, mids, is_core = [], [], []
        for k, train in folds:
            test = duel.filter(pl.col("mi") == k)
            tp = cd._prep(test, elo_of)
            if not tp["y"]:
                continue
            m_gd = cd.fit_card_model(train, elo_of, tau_d=args.tau_d)
            m_d = cd.fit_card_model(train, elo_of, tau_d=args.tau_d, use_g=False)
            P["taban"].extend(cd.predict_logit(m_gd, tp, use_g=False, use_d=False))
            P["taban+g"].extend(cd.predict_logit(m_gd, tp, use_g=True, use_d=False))
            P["taban+g+d"].extend(cd.predict_logit(m_gd, tp, use_g=True, use_d=True))
            P["taban+d"].extend(cd.predict_logit(m_d, tp, use_g=False, use_d=True))
            ys.extend(tp["y"]); mids.extend(tp["mid"])
            is_core.extend([(a in core) or (b in core) for a, b in zip(tp["a"], tp["b"], strict=True)])
        y, m, cr = np.array(ys), np.array(mids), np.array(is_core)
        z = {n: np.clip(np.array(v), -rt.MAX_LOGIT, rt.MAX_LOGIT) for n, v in P.items()}
        L = {n: ll(sigmoid(v), y) for n, v in z.items()}
        print(f"\n=== {split}: {len(y)} düello, {len(np.unique(m))} test maçı, çekirdek düellosu {int(cr.sum())} ===")
        print(f"log-kayıp: " + "  ".join(f"{n} {L[n].mean():.4f}" for n in names) + f"  yazı-tura 0.6931")
        for a, b in (("taban", "taban+g"), ("taban", "taban+g+d"), ("taban+g", "taban+g+d"), ("taban", "taban+d")):
            diff = L[a] - L[b]
            r_all, r_core = boot(diff, m), boot(diff, m, cr) if cr.sum() > 30 else (float("nan"),) * 3
            print(f"  {b:10s} vs {a:8s}: tümü {r_all[0]:+.4f} [{r_all[1]:+.4f}, {r_all[2]:+.4f}]"
                  f" | çekirdek {r_core[0]:+.4f} [{r_core[1]:+.4f}, {r_core[2]:+.4f}]")
        for n in names[1:]:
            print(f"  eğim {n}: {slope(z[n], y):.3f} (ofsetsiz); artımın eğimi (taban ofset): "
                  f"{slope(z[n] - z['taban'], y, z['taban']):.3f}")
        results[split] = dict(y=y, z=z)
    # Duyarlılık: tau_d (sadece ileri bölme, bilgi için)
    print("\n--- tau_d duyarlılığı (ileri bölme, taban+g+d vs taban+g) ---")
    for t in (0.1, 0.2, 0.3, 0.5):
        zs, zg, yy, mm = [], [], [], []
        for k, train in split_of["ileri"]:
            test = duel.filter(pl.col("mi") == k)
            tp = cd._prep(test, elo_of)
            if not tp["y"]:
                continue
            m_ = cd.fit_card_model(train, elo_of, tau_d=t)
            zs.extend(cd.predict_logit(m_, tp, use_g=True, use_d=True))
            zg.extend(cd.predict_logit(m_, tp, use_g=True, use_d=False))
            yy.extend(tp["y"]); mm.extend(tp["mid"])
        y_, m_ = np.array(yy), np.array(mm)
        diff = ll(sigmoid(np.array(zg)), y_) - ll(sigmoid(np.array(zs)), y_)
        r = boot(diff, m_)
        print(f"  tau_d={t}: {r[0]:+.4f} [{r[1]:+.4f}, {r[2]:+.4f}]")


if __name__ == "__main__":
    main()
