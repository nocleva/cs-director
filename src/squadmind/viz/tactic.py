"""Taktik tahtası sayfasının verisi: strat dosyası (plans/strats/*.json) + takımın verisi.

Strat elle yazılır (oyuncu anahtar kareleri, utility, görevler, karar dalları); geri kalan
her şey veriden üretilir: harita çizimi ve rotalar navmesh'ten, oyuncu kareleri yol
üzerinden, rakip spotları takımın kendi round'larından (biz bu taraftayken rakibin 15-25.
saniyede durduğu yerler), sekme başındaki sayılar takımın pistol / 2. round sonuçlarından.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from squadmind.analysis.economy import pistol_conversion
from squadmind.analysis.maps import BOARD_PLACES
from squadmind.analysis.player_metrics import DemoData, load_demo_data
from squadmind.analysis.team_tactics import team_rounds
from squadmind.config import PROJECT_ROOT
from squadmind.viz.board import (
    SMOKE_RADIUS, TAKTIK, Board, Spot, _offset, _Projection, _route, _smooth_path,
    map_polygons, merge_spots, render_board,
)
from squadmind.viz.playback import Actor, Keyframe, actor_frames

STRATS_DIR = PROJECT_ROOT / "plans" / "strats"
WIDTH = 560                 # tahtanın piksel genişliği (tasarım: 560 px kart)
DT = 0.1                    # oynatma karesi (sn)
SPOT_WINDOW = (15, 25)      # rakip spotları: round'un bu saniyeleri
SPOT_CELL = 150             # spot ızgarası (oyun birimi)
MOLLY_RADIUS = 90.0


@dataclass
class Strat:
    """plans/strats/<id>.json içeriği (alan adları dosyadakiyle aynı)."""
    id: str
    raw: dict[str, Any]

    @property
    def map_name(self) -> str:
        return self.raw["map"]

    @property
    def side(self) -> str:
        return self.raw.get("side", "T")

    @property
    def tabs(self) -> list[dict[str, Any]]:
        return self.raw.get("tabs", [])


def load_strats(folder: Path = STRATS_DIR, map_name: str | None = None) -> list[Strat]:
    """Klasördeki stratlar (dosya adı sırasıyla); harita verilirse sadece o harita."""
    out = []
    for path in sorted(folder.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if map_name is None or raw.get("map") == map_name:
            out.append(Strat(raw.get("id", path.stem), raw))
    return out


def find_tab(strats: list[Strat], tab_id: str | None) -> tuple[Strat, dict[str, Any]] | None:
    """Sekme kimliğine göre (strat, sekme); kimlik yoksa / bulunmazsa ilk sekme."""
    pairs = [(s, t) for s in strats for t in s.tabs]
    for s, t in pairs:
        if t["id"] == tab_id:
            return s, t
    return pairs[0] if pairs else None


# --- veriden gelen kısımlar ---------------------------------------------------------

def enemy_spots(d: DemoData, steam_id: str, side: str) -> tuple[list[Spot], int]:
    """Takım `side` tarafındayken rakibin SPOT_WINDOW saniyelerinde en sık durduğu yerler.

    Pay = o noktada görülen (round, oyuncu) sayısı / takımın o taraftaki round sayısı.
    Döner: (birleştirilmiş spotlar, round sayısı).
    """
    tr = (team_rounds(d, steam_id).filter(pl.col("side") == side)
          .select("match_id", "round_no"))
    if tr.height == 0:
        return [], 0
    enemy = "CT" if side == "T" else "T"
    pos = (d.positions.cast({"round_no": pl.Int64}).join(tr, on=["match_id", "round_no"])
           .filter((pl.col("side") == enemy)
                   & pl.col("time_in_round").is_between(*SPOT_WINDOW)))
    grid = (pos.with_columns((pl.col("x") / SPOT_CELL).floor().alias("gx"),
                             (pl.col("y") / SPOT_CELL).floor().alias("gy"))
            .group_by("gx", "gy")
            .agg(pl.col("x").mean(), pl.col("y").mean(),
                 pl.struct("match_id", "round_no", "player").n_unique().alias("n")))
    raw = [Spot((r["x"], r["y"]), r["n"] / tr.height, f"%{100 * r['n'] / tr.height:.0f}")
           for r in grid.sort("n", descending=True).head(40).iter_rows(named=True)]
    return merge_spots(raw), tr.height


def side_stats(d: DemoData, steam_id: str, side: str) -> dict[str, dict[str, int]]:
    """Sekmelerin sayıları: o taraftaki pistol ve pistol sonrası 2. round.

    Döner: {"pistol": {won, n}, "r2_after_win": {won, n}, "r2_after_loss": {won, n}}
    (taraf bir yarı boyunca sabit: tarafa göre süzmek yarıları bölmez)
    """
    tr = team_rounds(d, steam_id).filter(pl.col("side") == side)
    out = {"pistol": {"won": 0, "n": 0}, "r2_after_win": {"won": 0, "n": 0},
           "r2_after_loss": {"won": 0, "n": 0}}
    if tr.height == 0:
        return out
    for r in pistol_conversion(tr).iter_rows(named=True):
        key = "r2_after_win" if r["pistol_won"] else "r2_after_loss"
        out[key] = {"won": int(r["r2_wins"] or 0), "n": int(r["halves"])}
        out["pistol"]["n"] += int(r["halves"])
        if r["pistol_won"]:
            out["pistol"]["won"] += int(r["halves"])
    return out


# --- sayfa verisi ---------------------------------------------------------------------

def _molly_polygon(cx: float, cy: float, r: float) -> str:
    """Molotof için düzensiz kenar: sabit formül, her yüklemede aynı şekil."""
    pts = []
    for i in range(18):
        ang = 2 * math.pi * i / 18
        rr = r * (0.78 + 0.22 * (0.5 + 0.5 * math.sin(i * 2.7 + 1.3) * math.cos(i * 1.9)))
        pts.append(f"{cx + rr * math.cos(ang):.1f},{cy + rr * math.sin(ang):.1f}")
    return " ".join(pts)


def build_board(strat: Strat, spots: list[Spot], width: int = WIDTH) -> tuple[str, dict[str, Any]]:
    """Statik arka plan SVG'si + oynatılacak veri (oyuncu kareleri, rotalar, utility).

    Rakip spotları sayfada çizilir (nabız animasyonu), arka planda değil.
    """
    raw, map_name = strat.raw, strat.map_name
    pad = int(28 * width / 1000)
    board = Board(map_name, places=BOARD_PLACES.get(map_name, {}),
                  target_places=[tuple(p) for p in raw.get("target_places", [])],
                  target_radius=raw.get("target_radius", 430))
    background = render_board(board, width=width, pad=pad, theme=TAKTIK)
    P = _Projection(map_polygons(map_name), width, pad)

    def px(p) -> list[float]:
        x, y = P(p[0], p[1])
        return [round(x, 1), round(y, 1)]

    duration = float(raw["duration"])
    players = []
    for p in raw["players"]:
        keys = [Keyframe(t, tuple(pos)) for t, pos in p["keys"]]
        actor = Actor(p["label"], p["name"], keys=keys, lane=p.get("lane", 0))
        # Rota: art arda aynı nokta (bekleme) tek nokta sayılır
        world: list[tuple[float, float, float]] = []
        for k in keys:
            if not world or k.pos != world[-1]:
                world.append(k.pos)
        route = [P(x, y) for x, y in _route(map_name, world)]
        players.append({
            "label": p["label"], "name": p["name"], "mark": p.get("mark", "dot"),
            "role": p.get("role", ""), "kol": p.get("kol", ""), "hint": p.get("hint", ""),
            "path": _smooth_path(_offset(route, actor.lane)),
            "frames": [px(f) for f in actor_frames(map_name, actor, duration, DT)],
        })

    by_label = {p["label"]: p for p in players}
    utility = []
    for u in raw.get("utility", []):
        frames = by_label[u["thrower"]]["frames"]
        item = {"kind": u["kind"], "t": u["t"], "land": px(u["land"]), "label": u["label"],
                "thrower": u["thrower"], "dx": u.get("dx", 12), "dy": u.get("dy", -20),
                # atış noktası: atanın o anki konumu
                "from": frames[min(int(round(u["t"] / DT)), len(frames) - 1)],
                "r": round((SMOKE_RADIUS if u["kind"] == "smoke" else MOLLY_RADIUS) * P.s, 1)}
        if u["kind"] == "molly":
            item["poly"] = _molly_polygon(*item["land"], item["r"])
        utility.append(item)

    decisions = {p["label"]: {"name": p["name"], "task": p.get("task", ""),
                              "timeline": p.get("timeline", [[0, "", ""]]),
                              "branches": p.get("branches", [])}
                 for p in raw["players"]}
    data = {"w": width, "h": P.height, "dt": DT, "duration": duration,
            "entry": raw.get("entry", duration), "players": players, "utility": utility,
            "spots": [{"xy": px(s.pos), "label": s.label, "share": round(s.share, 3)}
                      for s in spots],
            "decisions": decisions, "events": raw.get("events", [])}
    return background, data


def stat_line(tab: dict[str, Any], stats: dict[str, dict[str, int]], side: str) -> str:
    """Sekme başlığının altındaki veri satırı; örnek yoksa bunu açıkça söyler."""
    s = stats.get(tab.get("stat", ""))
    if not s:
        return ""
    label = {"pistol": f"{side} pistol round'u",
             "r2_after_win": f"{side} pistolü kazanınca 2. round",
             "r2_after_loss": f"{side} pistolü kaybedince 2. round"}[tab["stat"]]
    if s["n"] == 0:
        return f"{label}: veri yok"
    return f"{label}: {s['won']}/{s['n']} kazanıldı (%{100 * s['won'] / s['n']:.0f})"


# Sayfa verisi önbelleği: harita + rotalar + spotlar birkaç saniye sürer; yeni demo
# işlenince (demo sayısı değişince) ya da strat dosyası değişince yeniden hesaplanır
_CACHE: dict[tuple, dict[str, Any]] = {}


def tactic_page(con: duckdb.DuckDBPyConnection, owner: str, strat: Strat) -> dict[str, Any]:
    """Sayfa için her şey: arka plan, oynatma verisi, spotlar, sekme sayıları.

    `owner`: takımı belirleyen oyuncunun FACEIT nickname'i (büyük / küçük harf fark etmez).
    """
    row = con.execute("SELECT steam_id FROM players WHERE lower(nickname) = lower(?)",
                      [owner]).fetchone()
    if not row or not row[0]:
        raise LookupError(f"'{owner}' veritabanında yok ya da Steam hesabı bağlı değil.")
    steam_id = row[0]
    demo_count = con.execute(
        "SELECT count(*) FROM matches WHERE demo_status = 'processed' AND map = ?",
        [strat.map_name]).fetchone()[0]
    key = (strat.id, steam_id, demo_count, json.dumps(strat.raw, sort_keys=True))
    if key not in _CACHE:
        d = load_demo_data(con, strat.map_name)
        spots, side_rounds = enemy_spots(d, steam_id, strat.side)
        background, data = build_board(strat, spots)
        _CACHE.clear()  # tek takım, tek strat dosyası grubu: eski girişleri tutmaya gerek yok
        _CACHE[key] = {"background": background, "data": data, "side_rounds": side_rounds,
                       "stats": side_stats(d, steam_id, strat.side), "demo_count": demo_count}
    return _CACHE[key]
