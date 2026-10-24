"""Tek gücün (ELO + öğrenilmiş bileşenler) düello tahminine katkısı: sınama.

Kullanım:  PYTHONPATH=src python scripts/eval_power.py [--db <duckdb>] [--map de_dust2] [--hist <pkl>]

Taban = arenadaki ana olasılık: β·ΔELO (maç anındaki seviye ortası) + onaylı sade silah (can: yok,
100/100). Aday = taban + Σ γ_k·Δz_k (stat eki, demo düello artığı, açılış, trade, clutch, öldürmeden
hasar; ayrıca silah kayması ayrı satır). Bileşenler EĞİTİMDE bir-maç-dışarıda çapraz-uydurmalı, teste
yalnız eğitim maçlarından; γ eğitimde ridge (ön bilgi N(0, 0,15²)) ile öğrenilir. İki bölme:
  ileri   : ilk k maçla eğit, (k+1). maçı test et (k = 6 … son-1)
  bir-maç-dışarıda: her maç sırayla dışarıda, kalan hepsiyle eğit
Fark = taban log-kaybı − aday log-kaybı (pozitif = aday iyi); %90 aralık maç-kümeli bootstrap.
Kabul (profesör): alt sınır > 0 iki bölmede ve kalibrasyon eğimi 0,9-1,1.
Sonunda tüm veriden öğrenilen ağırlıklar ve çekirdek oyuncuların gücü (canlı ELO) yazılır.
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

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
RNG = np.random.default_rng(11)


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def boot(diff: np.ndarray, mids: np.ndarray, sel: np.ndarray | None = None, b: int = 2000
         ) -> tuple[float, float, float]:
    """Ortalama fark ve maç-kümeli %90 aralık."""
    if sel is not None:
        diff, mids = diff[sel], mids[sel]
    ids = np.unique(mids)
    s = np.array([diff[mids == m].sum() for m in ids])
    n = np.array([(mids == m).sum() for m in ids])
    out = [s[k].sum() / n[k].sum() for k in (RNG.integers(0, len(ids), len(ids)) for _ in range(b))]
    lo, hi = np.percentile(out, [5, 95])
    return float(diff.mean()), float(lo), float(hi)


def slope(z: np.ndarray, y: np.ndarray, off: np.ndarray | None = None) -> float:
    """Kalibrasyon eğimi: y ~ off + s·z (lojistik); s = 1 iyi."""
    beta, _ = rt._fit_logistic(z.reshape(-1, 1), np.zeros(len(y)) if off is None else off, y, 1e-6)
    return float(beta[0])


def load_inputs(con, client, map_name: str, hist_path: Path | None):
    """DemoData, düellolar, maç sırası, (maç, oyuncu) → seviye ELO'su ve stat eki, popülasyon."""
    d = load_demo_data(con, map_name)
    duel = du.duels(d, map_name)
    order = d.match_maps.sort("date")["match_id"].to_list()
    duel = duel.with_columns(pl.col("match_id").replace_strict({m: i for i, m in enumerate(order)}).alias("mi"))
    if hist_path and hist_path.exists():
        hist = pickle.loads(hist_path.read_bytes())
    else:
        hist = {}
        for mid in order:
            det = client.get_match_details(mid)
            for team in (det.get("teams") or {}).values():
                for p in team.get("roster") or []:
                    items = client.get_player_match_stats(p["player_id"], to_ms=(int(det["finished_at"]) + 60) * 1000)
                    ids = [it.get("Match Id") for it in items]
                    prior = items[ids.index(mid) + 1:] if mid in ids else \
                        [it for it in items if (it.get("Match Finished At") or 0) < int(det["started_at"]) * 1000]
                    hist[(mid, p.get("game_player_id"))] = (rt.summarize_history(prior), p.get("game_skill_level"))
        if hist_path:
            hist_path.write_bytes(pickle.dumps(hist))
    pop = rt.fit_population((h, rt.level_elo(lv)[0]) for h, lv in hist.values())
    elo_of, stat_of = cd.power_inputs(hist, pop)
    return d, duel, order, elo_of, stat_of, pop


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--map", default="de_dust2")
    ap.add_argument("--hist", default=None, help="FACEIT geçmiş önbelleği (pickle)")
    ap.add_argument("--start", type=int, default=6)
    ap.add_argument("--gamma-sd", type=float, default=cd.GAMMA_SD)
    ap.add_argument("--no-core", action="store_true")
    args = ap.parse_args()
    con = duckdb.connect(args.db, read_only=True)
    with FaceitClient() as client:
        d, duel, order, elo_of, stat_of, pop = load_inputs(con, client, args.map,
                                                            Path(args.hist) if args.hist else None)
        counts = cd.component_counts(d)
        sd = cd.stat_scale(stat_of)
        core = set(d.roster.filter(pl.col("nickname").is_in(CORE))["steam_id"].to_list())
        P = cd.POWER_COMPONENTS
        specs: dict[str, tuple[str, ...]] = {"taban": ()}
        specs["güç (6 bileşen)"] = P
        specs["güç + silah kayması"] = (*P, "weapon")
        for c in cd.DESIGN_COLS:
            specs[f"taban + {c}"] = (c,)
        for c in P:
            specs[f"güç − {c}"] = tuple(x for x in P if x != c)
        names = list(specs)
        splits = {"ileri": [(k, duel.filter(pl.col("mi") < k)) for k in range(args.start, len(order))],
                  "bir-maç-dışarıda": [(k, duel.filter(pl.col("mi") != k)) for k in range(len(order))]}
        for split, folds in splits.items():
            Zp: dict[str, list[float]] = {n: [] for n in [*names, "saf ELO"]}
            ys, mids, is_core = [], [], []
            for k, train in folds:
                test = duel.filter(pl.col("mi") == k)
                p = cd._prep(test, elo_of)
                if not p["y"]:
                    continue
                tmids = set(train["match_id"].to_list())
                Dtr = cd.crossfit_design(train, counts, elo_of, stat_of, sd)
                state = cd.build_state(counts, tmids, train, elo_of, sd)
                Dte = cd.design(state, p, stat_of)
                for n, cols in specs.items():
                    b, _ = cd.fit_joint(Dtr, cols, args.gamma_sd)
                    Zp[n].extend(cd.predict_joint(Dte, b, cols))
                b0, _ = rt._fit_logistic(Dtr.dl.reshape(-1, 1), np.zeros(len(Dtr.y)), Dtr.y, rt.RIDGE)
                Zp["saf ELO"].extend(np.clip(b0[0] * Dte.dl, -rt.MAX_LOGIT, rt.MAX_LOGIT))
                ys.extend(p["y"]); mids.extend(p["mid"])
                is_core.extend([(a in core) or (b_ in core) for a, b_ in zip(p["a"], p["b"], strict=True)])
            y, m, cr = np.array(ys), np.array(mids), np.array(is_core)
            z = {n: np.array(v) for n, v in Zp.items()}
            L = {n: ll(sigmoid(v), y) for n, v in z.items()}
            print(f"\n=== {split}: {len(y)} düello, {len(np.unique(m))} test maçı, çekirdek düellosu {int(cr.sum())} ===")
            print("log-kayıp  " + "  ".join(f"{n} {L[n].mean():.4f}" for n in ("saf ELO", "taban", "güç (6 bileşen)")) + "  yazı-tura 0.6931")
            print(f"{'aday':26s} {'tabana göre kazanç (tümü)':34s} {'çekirdek':30s} eğim  artım-eğimi")
            for n in names[1:]:
                r_all = boot(L["taban"] - L[n], m)
                r_core = boot(L["taban"] - L[n], m, cr) if cr.sum() > 30 else (float("nan"),) * 3
                print(f"{n:26s} {r_all[0]:+.4f} [{r_all[1]:+.4f}, {r_all[2]:+.4f}]   "
                      f"{r_core[0]:+.4f} [{r_core[1]:+.4f}, {r_core[2]:+.4f}]  "
                      f"{slope(z[n], y):.3f}  {slope(z[n] - z['taban'], y, z['taban']):.3f}")
            r = boot(L["saf ELO"] - L["taban"], m)
            print(f"taban (ELO+sade silah) saf ELO'ya göre: {r[0]:+.4f} [{r[1]:+.4f}, {r[2]:+.4f}]")
            r = boot(L["saf ELO"] - L["güç (6 bileşen)"], m)
            print(f"güç (6 bileşen) saf ELO'ya göre:        {r[0]:+.4f} [{r[1]:+.4f}, {r[2]:+.4f}]")

        # Önsel duyarlılığı (ileri bölme): gamma_sd
        print("\n--- γ ön bilgi sapması duyarlılığı (ileri, güç vs taban) ---")
        for gs in (0.05, 0.15, 0.3, 0.6):
            zt, zg, yy, mm = [], [], [], []
            for k, train in splits["ileri"]:
                test = duel.filter(pl.col("mi") == k)
                p = cd._prep(test, elo_of)
                if not p["y"]:
                    continue
                Dtr = cd.crossfit_design(train, counts, elo_of, stat_of, sd)
                Dte = cd.design(cd.build_state(counts, set(train["match_id"].to_list()), train, elo_of, sd), p, stat_of)
                b, _ = cd.fit_joint(Dtr, P, gs); zg.extend(cd.predict_joint(Dte, b, P))
                b, _ = cd.fit_joint(Dtr, (), gs); zt.extend(cd.predict_joint(Dte, b, ()))
                yy.extend(p["y"]); mm.extend(p["mid"])
            y_, m_ = np.array(yy), np.array(mm)
            r = boot(ll(sigmoid(np.array(zt)), y_) - ll(sigmoid(np.array(zg)), y_), m_)
            print(f"  gamma_sd={gs}: {r[0]:+.4f} [{r[1]:+.4f}, {r[2]:+.4f}]")

        # Tüm veriden ağırlıklar
        pm = cd.fit_power(duel, counts, elo_of, stat_of, gamma_sd=args.gamma_sd)
        print(f"\n=== Tüm veri: {pm.n_duels} düello, β = {pm.beta:.4f} ± {pm.beta_sd:.4f} / 100 ELO ===")
        for i, c in enumerate(pm.cols):
            se = float(np.sqrt(max(pm.gamma_cov[i, i], 0)))
            g = pm.gamma[c]
            print(f"  {c:8s} γ = {g:+.4f} ± {se:.4f} (%90 [{g - rt.Z90 * se:+.3f}, {g + rt.Z90 * se:+.3f}])"
                  f"  1 sd = {pm.elo_per_sd(c):+.0f} ELO  ön bilgi: {pm.state.priors.get(c)}")
        if args.no_core:
            return
        wc = cd.weapon_counts(duel)
        print("\n=== Çekirdek oyuncular (canlı ELO) ===")
        for nick in CORE:
            try:
                prof = client.get_player_by_nickname(nick)
                pid = prof["player_id"]
                live = client.get_player_live(pid)
                game = (live.get("games") or {}).get("cs2") or {}
                hist = rt.summarize_history(client.get_player_match_stats(pid))
                kw = {"elo": rt._num(game.get("faceit_elo")), "level": game.get("skill_level")}
                f0 = rt.player_features(hist, pop, kappa=0.0, **kw)
                f1 = rt.player_features(hist, pop, **kw)
                sid = live.get("steam_id_64") or prof.get("steam_id_64")
                card = cd.player_card(sid, feats=f0, counts=wc, desc=cd.descriptives(d, sid), nickname=nick,
                                      power_model=pm, stat_adj=f1.stat_adj)
                pw = card.power
                print(f"\n{nick}: {pw.formula()}  | puan {pw.score:.1f} [{pw.lo:.1f}, {pw.hi:.1f}]"
                      f"  ELO aralığı [{pw.lo_elo:.0f}, {pw.hi_elo:.0f}]")
                for part in pw.parts[1:]:
                    print(f"   {part.name:24s} {part.contribution_elo:+6.1f} ELO  z={part.z:+.2f}"
                          f" w={part.data_weight:.2f} n={part.n}")
                for c, w in card.weapons.items():
                    print(f"   silah {c:6s} {w.detail}  değer {w.value:.3f} [{w.lo:.3f}, {w.hi:.3f}] {w.label}")
            except Exception as e:  # noqa: BLE001 - tek oyuncu başarısız olursa diğerleri yazılsın
                print(f"{nick}: alınamadı ({e})")


if __name__ == "__main__":
    main()
