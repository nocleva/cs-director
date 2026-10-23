"""Peek hattı düelloları ve oyuncu gücü (taktik oluşturucunun çekirdeği).

Düello = rakibe kill (silahla; utility / bıçak / dünya hariç). Saldıranın ve kurbanın bölgesi
bir peek hattına (`maps.PEEK_LINES`) düşüyorsa düello o hatta sayılır. Kazanan saldırandır.

Güç, az örnekte uçuk çıkmasın diye iki kademede küçültülür (shrinkage):
1. Oyuncunun genel düello oranı → %50'ye `OVERALL_PRIOR` düello ağırlığında çekilir.
2. Oyuncunun bir hat + taraftaki oranı → (1)'e `LINE_PRIOR` düello ağırlığında çekilir.
Sonuç "etki" (log-odds) olarak tutulur: oyuncunun o hatta o tarafın ortalamasından ne kadar
iyi / kötü olduğu. İki oyuncunun karşılaşması: taban (hattın o taraf için ortalaması) + A'nın
etkisi − B'nin etkisi.

Sınırlar (dürüstlük): sadece kill'le biten düellolar görülür (hasar alıp kaçmak sayılmaz);
"peeker / tutan" ayrımı yok; mesafe dilimi raporlanır ama modele girmez (örnek az).

Sınama (23.10.2026, ileriye kayan pencere, 12 test maçı): bu modelin `matchup`'ı yazı-turayı
geçemiyor (log-kayıp farkı −0,003); hat × taraf tabanı da yazı-turadan kötü. Karşılaşma
olasılığı için `analysis/rating.py` (FACEIT ELO + stat puanı) kullanılır; bu modül düello
çıkarma (`duels`, `line_of`) ve betimleyici hat sayıları için kalır.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from squadmind.analysis.maps import PEEK_LINES
from squadmind.analysis.player_metrics import (
    PRIMARY_CATEGORIES,
    WEAPON_CATEGORIES,
    DemoData,
    _weapon_category,
)

# Küçültme ağırlıkları (düello sayısı cinsinden)
OVERALL_PRIOR = 20.0
LINE_PRIOR = 8.0
# Mesafe dilimleri (oyun birimi; 1 birim ≈ 1,9 cm). 2.911 Dust2 kill'inde çeyrekler ~450 / 720 / 1.090
DIST_BANDS = ((500.0, "yakın"), (1100.0, "orta"), (math.inf, "uzak"))
# Güven etiketi: hat + taraftaki düello sayısı
CONFIDENCE = ((5, "az"), (15, "orta"), (math.inf, "iyi"))
_GUN_WEAPONS = [w for cat, ws in WEAPON_CATEGORIES.items() if cat != "utility" for w in ws]


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def band_of(dist: float) -> str:
    for limit, name in DIST_BANDS:
        if dist < limit:
            return name
    return DIST_BANDS[-1][1]


def line_of(map_name: str, place_a: str | None, place_b: str | None) -> str | None:
    """İki bölgenin düştüğü peek hattı (yön fark etmez); yoksa None."""
    for line_id, _, set_a, set_b in PEEK_LINES.get(map_name, []):
        if (place_a in set_a and place_b in set_b) or (place_a in set_b and place_b in set_a):
            return line_id
    return None


def duels(d: DemoData, map_name: str) -> pl.DataFrame:
    """Haritadaki silahlı rakip kill'leri, hat ve mesafe dilimiyle.

    Kolonlar: match_id, round_no, winner, loser, winner_side, loser_side, winner_place,
    loser_place, weapon, dist, band, line (hatta değilse boş), winner_cls, loser_cls
    (iki oyuncunun O ROUND kill anına kadar en çok tuttuğu birincil silahın sınıfı; konum
    kaydı yoksa "pistol". Kill'den önce bilinen bilgi: `rating.fit_weapon_effect` girdisi)
    """
    ids = d.match_maps.filter(pl.col("map") == map_name)["match_id"].to_list()
    k = d.kills.filter(pl.col("match_id").is_in(ids)
                       & (pl.col("attacker_side") != pl.col("victim_side"))
                       & pl.col("weapon").is_in(_GUN_WEAPONS)
                       & pl.col("attacker_x").is_not_null() & pl.col("victim_x").is_not_null())
    rows = []
    for r in k.iter_rows(named=True):
        dist = math.dist((r["attacker_x"], r["attacker_y"]), (r["victim_x"], r["victim_y"]))
        rows.append({
            "match_id": r["match_id"], "round_no": r["round_no"],
            "winner": r["attacker"], "loser": r["victim"],
            "winner_side": r["attacker_side"], "loser_side": r["victim_side"],
            "winner_place": r["attacker_place"], "loser_place": r["victim_place"],
            "weapon": r["weapon"], "dist": round(dist, 1), "band": band_of(dist),
            "line": line_of(map_name, r["attacker_place"], r["victim_place"]),
            "_t": r["time_in_round"], "_i": len(rows),
        })
    schema = {"match_id": pl.String, "round_no": pl.Int64, "winner": pl.String, "loser": pl.String,
              "winner_side": pl.String, "loser_side": pl.String, "winner_place": pl.String,
              "loser_place": pl.String, "weapon": pl.String, "dist": pl.Float64, "band": pl.String,
              "line": pl.String, "_t": pl.Float64, "_i": pl.Int64}
    out = pl.DataFrame(rows, schema=schema)
    out = out.with_columns(_main_class(d, out, "winner").alias("winner_cls"),
                           _main_class(d, out, "loser").alias("loser_cls"))
    return out.drop("_t", "_i")


def _main_class(d: DemoData, du: pl.DataFrame, who: str) -> pl.Series:
    """Düellodaki `who` ("winner" / "loser") oyuncusunun kill anına kadarki ana silah sınıfı."""
    if du.height == 0 or "active_weapon" not in d.positions.columns:
        return pl.Series([ "pistol"] * du.height, dtype=pl.String)
    pos = d.positions.select(
        "match_id", pl.col("round_no").cast(pl.Int64), pl.col("player").alias(who),
        pl.col("time_in_round").alias("_ts"), _weapon_category("active_weapon").alias("_cat"),
    ).filter(pl.col("_cat").is_in(PRIMARY_CATEGORIES))
    held = (
        du.select("_i", "match_id", "round_no", who, "_t").join(pos, on=["match_id", "round_no", who])
        .filter(pl.col("_ts") <= pl.col("_t") + 0.01)
        .group_by("_i", "_cat").agg(pl.len().alias("_n"))
        .sort(["_n", "_cat"], descending=[True, False])
        .group_by("_i", maintain_order=True).first()
    )
    return du.select("_i").join(held, on="_i", how="left")["_cat"].fill_null("pistol")


def _participations(du: pl.DataFrame) -> pl.DataFrame:
    """Her düello iki satır: kazanan (won=1) ve kaybeden (won=0), kendi tarafıyla."""
    w = du.select("line", "band", pl.col("winner").alias("player"),
                  pl.col("winner_side").alias("side"), pl.lit(1).alias("won"))
    l_ = du.select("line", "band", pl.col("loser").alias("player"),
                   pl.col("loser_side").alias("side"), pl.lit(0).alias("won"))
    return pl.concat([w, l_])


def strengths(du: pl.DataFrame) -> dict[str, Any]:
    """Model: hat + taraf tabanları, oyuncu genel oranı ve hat + taraf etkileri.

    Döner: {"base": {(line, side): p}, "overall": {player: (p, n)},
            "lines": {(player, line, side): {"n", "wins", "raw", "effect", "strength", "conf"}}}
    """
    part = _participations(du)
    on_line = part.filter(pl.col("line").is_not_null())
    base = {(r["line"], r["side"]): r["p"] for r in
            on_line.group_by("line", "side").agg(pl.col("won").mean().alias("p")).iter_rows(named=True)}
    overall = {}
    for r in part.group_by("player").agg(pl.col("won").sum().alias("w"), pl.len().alias("n")).iter_rows(named=True):
        overall[r["player"]] = ((r["w"] + OVERALL_PRIOR * 0.5) / (r["n"] + OVERALL_PRIOR), r["n"])
    lines = {}
    agg = on_line.group_by("player", "line", "side").agg(pl.col("won").sum().alias("w"), pl.len().alias("n"))
    for r in agg.iter_rows(named=True):
        b = base[(r["line"], r["side"])]
        # Ön bilgi: oyuncunun genel gücü bu hattın taban oranına uygulanır (log-odds toplamı)
        prior = _sigmoid(_logit(b) + _logit(overall[r["player"]][0]))
        s = (r["w"] + LINE_PRIOR * prior) / (r["n"] + LINE_PRIOR)
        lines[(r["player"], r["line"], r["side"])] = {
            "n": r["n"], "wins": r["w"], "raw": r["w"] / r["n"], "strength": s,
            "effect": _logit(s) - _logit(b),
            "conf": next(name for limit, name in CONFIDENCE if r["n"] < limit),
        }
    return {"base": base, "overall": overall, "lines": lines}


def effect(model: dict[str, Any], player: str, line: str, side: str) -> float:
    """Oyuncunun hat + taraftaki etkisi; o hatta hiç düellosu yoksa genel gücünden."""
    hit = model["lines"].get((player, line, side))
    if hit:
        return hit["effect"]
    p, _ = model["overall"].get(player, (0.5, 0))
    return _logit(p)


def matchup(model: dict[str, Any], a: str, side_a: str, b: str, line: str) -> float | None:
    """A (side_a tarafında) ile B'nin bu hatta karşılaşmasında A'nın kazanma olasılığı."""
    side_b = "CT" if side_a == "T" else "T"
    base = model["base"].get((line, side_a))
    if base is None:
        return None
    return _sigmoid(_logit(base) + effect(model, a, line, side_a) - effect(model, b, line, side_b))
