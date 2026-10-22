"""Taktik oynatma verisi: oyuncuların saniye saniye konumu + utility zamanları.

Tahta (`board.py`) taktiğin durağan resmidir; bu modül aynı taktiği zamana yayar. Her
oyuncunun rotası anahtar karelerle verilir: (zaman, dünya noktası). İki anahtar kare
arasındaki yol navmesh'te yürüme yoludur (tahtadaki rotayla aynı) ve oyuncu bu yolu
iki kare arasındaki sürede sabit hızla yürür; karelerde bekleme (ör. flash'ı beklemek)
aynı noktayı iki kez vererek yazılır.

Çıktı sayfanın doğrudan oynatabileceği bir sözlüktür (piksel koordinatları, `dt`
aralıklı kareler); hesap Python'da, sayfa sadece kareleri gösterir.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from squadmind.viz.board import (
    SMOKE_RADIUS,
    Board,
    Theme,
    KESKIN,
    T,
    _Projection,
    _route,
    map_polygons,
    render_board,
)


@dataclass
class Keyframe:
    t: float                                  # saniye (freeze bitişinden)
    pos: tuple[float, float, float]           # dünya koordinatı


@dataclass
class Actor:
    label: str
    name: str
    role: str = T                             # "t" / "support"
    keys: list[Keyframe] = field(default_factory=list)
    lane: float = 0.0                         # tahtadaki gibi yanal kaydırma (px)


@dataclass
class TimedUtility:
    kind: str                                 # smoke / flash / molly
    t: float                                  # patladığı an
    land: tuple[float, float, float]
    label: str
    thrower: str = ""                         # atan oyuncunun etiketi (1-5)


def _cumulative(pts: list[tuple[float, float]]) -> list[float]:
    acc = [0.0]
    for a, b in zip(pts, pts[1:], strict=False):
        acc.append(acc[-1] + math.dist(a, b))
    return acc


def _at_distance(pts: list[tuple[float, float]], acc: list[float], d: float) -> tuple[float, float]:
    """Çizgi üzerinde baştan `d` uzaklıktaki nokta."""
    if d <= 0:
        return pts[0]
    for i in range(1, len(pts)):
        if acc[i] >= d:
            seg = acc[i] - acc[i - 1] or 1.0
            f = (d - acc[i - 1]) / seg
            return (pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f,
                    pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f)
    return pts[-1]


def actor_frames(map_name: str, actor: Actor, duration: float, dt: float) -> list[tuple[float, float]]:
    """Oyuncunun her `dt` saniyedeki dünya (x, y) konumu, 0'dan `duration`'a kadar."""
    keys = sorted(actor.keys, key=lambda k: k.t)
    legs = []  # (t0, t1, yol noktaları, kümülatif uzunluk)
    for a, b in zip(keys, keys[1:], strict=False):
        pts = [(a.pos[0], a.pos[1])] if a.pos == b.pos else _route(map_name, [a.pos, b.pos])
        legs.append((a.t, b.t, pts, _cumulative(pts)))
    out = []
    n = int(round(duration / dt)) + 1
    for i in range(n):
        t = i * dt
        if t <= keys[0].t:
            out.append((keys[0].pos[0], keys[0].pos[1]))
            continue
        for t0, t1, pts, acc in legs:
            if t0 <= t <= t1:
                frac = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                out.append(_at_distance(pts, acc, frac * acc[-1]))
                break
        else:  # son kareden sonra: yerinde bekler
            out.append((keys[-1].pos[0], keys[-1].pos[1]))
    return out


def build_playback(map_name: str, actors: list[Actor], utility: list[TimedUtility],
                   duration: float, dt: float = 0.1, width: int = 660,
                   board: Board | None = None, theme: Theme = KESKIN) -> dict:
    """Sayfanın oynatacağı veri: arka plan SVG'si, oyuncu kareleri (px), utility (px).

    Arka plan, tahtanın oyuncusuz / utility'siz çizimidir (harita, bölge adları, hedef
    site, rakip spotları); hareketli her şey sayfada çizilir.
    """
    polys = map_polygons(map_name)
    P = _Projection(polys, width, pad=int(28 * width / 1000))
    bg_board = board or Board(map_name=map_name)
    bg_board = Board(map_name=map_name, places=bg_board.places, spots=bg_board.spots,
                     target_places=bg_board.target_places, target_radius=bg_board.target_radius)
    background = render_board(bg_board, width=width, pad=int(28 * width / 1000), theme=theme)

    def px(p: tuple[float, float]) -> list[float]:
        x, y = P(p[0], p[1])
        return [round(x, 1), round(y, 1)]

    players = []
    for a in actors:
        frames = actor_frames(map_name, a, duration, dt)
        players.append({
            "label": a.label, "name": a.name, "role": a.role,
            "frames": [px(f) for f in frames],
            # Tüm rotanın izi (soluk kesik çizgi): oynatırken yolun tamamı görünsün
            "trail": [px(f) for f in frames[:: max(1, int(0.5 / dt))]] + [px(frames[-1])],
        })
    util = []
    for u in utility:
        x, y = px((u.land[0], u.land[1]))
        util.append({"kind": u.kind, "t": u.t, "x": x, "y": y, "label": u.label,
                     "thrower": u.thrower,
                     "r": round(SMOKE_RADIUS * P.s, 1) if u.kind == "smoke" else
                     round(90 * P.s, 1) if u.kind == "molly" else 10})
    return {"width": width, "height": P.height, "dt": dt, "duration": duration,
            "background": background, "players": players, "utility": util}
