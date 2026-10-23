"""Desen etiketlerinin doğrulanması (Faz 2b): örnek round seçimi ve round tekrarı verisi.

`t_round_patterns` her T round'una kuralla bir desen verir (rush / execute / split /
girilmedi). Kuralın doğru çalışıp çalışmadığını bilmek için her desenden birkaç round, gerçek
oyuncu hareketleriyle oynatılıp insan tarafından işaretlenir; doğruluk oranı desen başına
çıkar. Örnek seçimi deterministik (maç + round özetine göre), sayfa her açılışta aynı
round'ları aynı sırayla gösterir.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import polars as pl

from squadmind.analysis.player_metrics import DemoData

PATTERNS = ("rush", "execute", "split", "girilmedi")
# Desen başına incelenecek round
PER_PATTERN = 5


def _key(match_id: str, round_no: int) -> str:
    """Sıralama anahtarı: rastgele görünen ama her çalıştırmada aynı sıra."""
    return hashlib.sha1(f"{match_id}:{round_no}".encode()).hexdigest()


def sample_rounds(tp: pl.DataFrame, per_pattern: int = PER_PATTERN) -> list[dict[str, Any]]:
    """Her desenden `per_pattern` round; desenler sırayla karışık (rush, execute, split, ...).

    `tp`: `t_round_patterns` çıktısı. Az round'u olan desenden olduğu kadar alınır.
    """
    groups = []
    for pat in PATTERNS:
        rows = tp.filter(pl.col("pattern") == pat).to_dicts()
        rows.sort(key=lambda r: _key(r["match_id"], int(r["round_no"])))
        groups.append(rows[:per_pattern])
    out = []
    for i in range(per_pattern):
        for g in groups:
            if i < len(g):
                out.append(g[i])
    return out


def round_replay(d: DemoData, match_id: str, round_no: int,
                 px: Callable[[float, float], tuple[float, float]]) -> dict[str, Any]:
    """Bir round'un oynatma verisi (piksel koordinatlı).

    players: [{name, side, pts: [[t, x, y], ...]}] (konum kayıtları 2 sn'de bir, ölünce biter)
    kills: [{t, x, y, victim_side, victim}] (ölüm yeri), plant: {t, site} ya da None,
    duration: son kaydın saniyesi.
    """
    pos = d.positions.filter((pl.col("match_id") == match_id)
                             & (pl.col("round_no").cast(pl.Int64) == round_no)).sort("tick")
    names = {r["steam_id"]: r["nickname"] for r in
             d.roster.filter(pl.col("match_id") == match_id).iter_rows(named=True)}
    players = []
    for (sid, side), g in pos.group_by(["player", "side"], maintain_order=True):
        pts = []
        for r in g.iter_rows(named=True):
            x, y = px(r["x"], r["y"])
            pts.append([round(r["time_in_round"], 1), round(x, 1), round(y, 1)])
        players.append({"name": names.get(sid, "?"), "side": side, "pts": pts})
    kills = []
    k = d.kills.filter((pl.col("match_id") == match_id)
                       & (pl.col("round_no").cast(pl.Int64) == round_no))
    for r in k.sort("time_in_round").iter_rows(named=True):
        if r.get("victim_x") is None:
            continue
        x, y = px(r["victim_x"], r["victim_y"])
        kills.append({"t": round(r["time_in_round"], 1), "x": round(x, 1), "y": round(y, 1),
                      "victim_side": r["victim_side"], "victim": names.get(r["victim"], "?")})
    duration = max([p["pts"][-1][0] for p in players if p["pts"]] + [k["t"] for k in kills] + [1.0])
    return {"players": players, "kills": kills, "duration": round(duration, 1)}


def accuracy(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Desen başına doğruluk: doğru / işaretlenen ("emin değilim" sayılmaz) ve düzeltmeler."""
    out = []
    for pat in PATTERNS:
        rs = [r for r in reviews if r["label"] == pat and r["verdict"] in ("dogru", "yanlis")]
        ok = sum(r["verdict"] == "dogru" for r in rs)
        fixes: dict[str, int] = {}
        for r in rs:
            if r["verdict"] == "yanlis" and r.get("corrected"):
                fixes[r["corrected"]] = fixes.get(r["corrected"], 0) + 1
        out.append({"pattern": pat, "reviewed": len(rs), "correct": ok, "fixes": fixes})
    return out
