"""Taktik oluşturucu: haritaya bırakılan oyuncunun o noktadaki gücü.

Bir nokta → bölge adı (place_name): navmesh alanlarında bölge adı yok; oyuncuların gerçek konum
kayıtlarından her CELL birimlik hücreye en sık görülen bölge atanır, nokta en yakın hücreyi alır.
Bölge → peek hatları (`maps.PEEK_LINES`), hat + taraf → oyuncunun gücü (`duels.strengths`).
Güç modeli ileride `rating` modülüyle değişebilir; arayüz bu modülün çıktısını kullanır.
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from squadmind.analysis.maps import PEEK_LINES
from squadmind.analysis.player_metrics import DemoData

# Bölge ızgarası hücresi (oyun birimi); en yakın hücre bu mesafeden uzaksa bölge bilinmiyor
CELL = 64.0
MAX_SNAP = 320.0


def place_grid(d: DemoData) -> list[tuple[float, float, float, str]]:
    """(x, y, z, bölge) hücreleri: her hücrede en sık bölge, ortalama konum ve yükseklik."""
    pos = d.positions.filter(pl.col("place_name").is_not_null() & (pl.col("place_name") != ""))
    cells = (
        pos.with_columns((pl.col("x") / CELL).floor().alias("gx"), (pl.col("y") / CELL).floor().alias("gy"))
        .group_by("gx", "gy")
        .agg(pl.col("x").mean(), pl.col("y").mean(), pl.col("z").mean(),
             pl.col("place_name").mode().sort().first().alias("place"))
    )
    return [(round(r["x"], 1), round(r["y"], 1), round(r["z"], 1), r["place"])
            for r in cells.iter_rows(named=True)]


def place_at(grid: list[tuple[float, float, float, str]], x: float, y: float) -> tuple[str | None, float | None]:
    """Noktanın bölgesi ve yaklaşık yüksekliği (en yakın hücre); çok uzaksa (None, None)."""
    best, best_d = None, MAX_SNAP
    for gx, gy, gz, place in grid:
        dd = math.hypot(gx - x, gy - y)
        if dd < best_d:
            best, best_d = (place, gz), dd
    return best if best else (None, None)


def lines_for_place(map_name: str, place: str | None) -> list[tuple[str, str]]:
    """Bölgenin dahil olduğu peek hatları: (hat kimliği, ad)."""
    if not place:
        return []
    return [(lid, name) for lid, name, a, b in PEEK_LINES.get(map_name, []) if place in a or place in b]


def player_cards(model: dict[str, Any], map_name: str, steam_ids: list[str]) -> dict[str, Any]:
    """Arayüze giden güç tablosu: oyuncu → genel güç ve hat + taraf kayıtları (az veri dahil)."""
    out: dict[str, Any] = {}
    for sid in steam_ids:
        p, n = model["overall"].get(sid, (None, 0))
        lines = {}
        for (pl_, line, side), v in model["lines"].items():
            if pl_ == sid:
                lines[f"{line}|{side}"] = {"n": v["n"], "wins": v["wins"],
                                           "strength": round(v["strength"], 3), "conf": v["conf"]}
        out[sid] = {"overall": None if p is None else round(p, 3), "duels": n, "lines": lines}
    return out


def line_names(map_name: str) -> dict[str, str]:
    return {lid: name for lid, name, _, _ in PEEK_LINES.get(map_name, [])}


def line_places(map_name: str) -> dict[str, list[str]]:
    """Hat → içerdiği bölge adları (arayüz noktanın hatlarını kendisi bulsun diye)."""
    return {lid: sorted(a | b) for lid, _, a, b in PEEK_LINES.get(map_name, [])}
