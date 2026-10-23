"""Düello olasılık modelinin (analysis/rating.py) zaman bölmeli sınaması — tekrar üretilebilir.

Kullanım:  uv run python scripts/eval_rating.py [--db <duckdb>] [--map de_dust2] [--start 6]
İlk k maçla eğitir, (k+1). maçı tahmin eder (k = start … son). FACEIT geçmişleri her oyuncu için
o maçtan ÖNCEKİ son 100 maçtır (önbellekli; ilk çalıştırmada ~10 çağrı / maç). DB salt-okunur
açılır; web sunucusu DB'yi kilitliyorsa bir kopyasını verin.
"""

from __future__ import annotations

import argparse

import duckdb
import numpy as np
import polars as pl

from squadmind.analysis import duels as du
from squadmind.analysis import rating as rt
from squadmind.analysis.player_metrics import load_demo_data
from squadmind.config import DB_PATH
from squadmind.faceit.client import FaceitClient

MODELS = {
    "%50 (yazı-tura)": dict(use=(False,) * 4),
    "hat×taraf ortalaması": dict(use=(False,) * 4, base="line", base_prior=0.0),
    "sadece seviye farkı": dict(use=(True, False, False, False)),
    "birleşik ELO (varsayılan)": {},
    "birleşik + demo artığı": dict(demo=True),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--map", default="de_dust2")
    ap.add_argument("--start", type=int, default=6)
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    d = load_demo_data(con, args.map)
    duel = du.duels(d, args.map)
    order = d.match_maps.sort("date")["match_id"].to_list()
    duel = duel.with_columns(pl.col("match_id").replace_strict({m: i for i, m in enumerate(order)}).alias("mi"))

    # Maç anındaki seviye + maçtan önceki geçmiş (build_context ile aynı kesme kuralı)
    hist: dict[tuple[str, str], tuple[rt.History, int | None]] = {}
    with FaceitClient() as client:
        for mid in order:
            det = client.get_match_details(mid)
            for team in det["teams"].values():
                for p in team["roster"]:
                    items = client.get_player_match_stats(p["player_id"], to_ms=(det["finished_at"] + 60) * 1000)
                    ids = [it.get("Match Id") for it in items]
                    prior = items[ids.index(mid) + 1:] if mid in ids else \
                        [it for it in items if (it.get("Match Finished At") or 0) < det["started_at"] * 1000]
                    hist[(mid, p["game_player_id"])] = (rt.summarize_history(prior), p.get("game_skill_level"))
    mi_of = dict(enumerate(order))
    preds: dict[str, list[float]] = {n: [] for n in [*MODELS, "mevcut duels.py"]}
    ys: list[float] = []
    for k in range(args.start, len(order)):
        pop = rt.fit_population((h, rt.level_elo(lv)[0]) for (m, _), (h, lv) in hist.items()
                                if order.index(m) <= k)
        cache: dict[tuple[str, str], rt.PlayerFeatures] = {}

        def feats(mid: str, sid: str, cache=cache, pop=pop) -> rt.PlayerFeatures:
            if (mid, sid) not in cache:
                h, lv = hist.get((mid, sid), (rt.History((), ()), None))
                cache[(mid, sid)] = rt.player_features(h, pop, level=lv)
            return cache[(mid, sid)]

        train, test = duel.filter(pl.col("mi") < k), duel.filter(pl.col("mi") == k)
        fitted = {n: rt.fit_model(train, feats, **kw) for n, kw in MODELS.items()}
        cur = du.strengths(train)
        for r in test.iter_rows(named=True):
            t_won = r["winner_side"] == "T"
            a, b = (r["winner"], r["loser"]) if t_won else (r["loser"], r["winner"])
            ys.append(float(t_won))
            for n, m in fitted.items():
                preds[n].append(rt.matchup(m, a, "T", b, r["line"], feats(mi_of[k], a), feats(mi_of[k], b)).p)
            pc = du.matchup(cur, a, "T", b, r["line"]) if r["line"] else None
            preds["mevcut duels.py"].append(pc if pc is not None else 0.5)
    y = np.array(ys)
    print(f"{len(order) - args.start} test maçı, {len(y)} düello")
    print(f"{'model':28s} log-kayıp  Brier   isabet")
    for n, p in preds.items():
        pa = np.array(p)
        print(f"{n:28s} {rt.log_loss(pa, y):.4f}   {rt.brier(pa, y):.4f}  {np.mean((pa > .5) == (y == 1)):.3f}")
    print("Kalibrasyon (birleşik ELO):")
    for c in rt.calibration(preds["birleşik ELO (varsayılan)"], y, 5):
        print(f"  tahmin {c['pred']:.3f} → gerçek {c['obs']:.3f} (n={c['n']})")


if __name__ == "__main__":
    main()
