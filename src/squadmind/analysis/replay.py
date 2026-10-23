"""Arena "Taktik" modu: TEK bir örnek akış (olay sırası), olasılık ya da skor YOK.

Profesör kararı (24.10.2026): `simulate.py` sınamayı geçmedi (silah sınıfı sayıları tabanından
kötü, rota katkısı kanıtlanamadı). Bu yüzden bu modül yalnız BETİMLER: bir tohumla tek bir
tekrar çalıştırılır ve ortaya çıkan olaylar (görüş başladı, düello: kim kimi vurdu, bomba kuruldu)
zamanlı konumlarla döner. Çıktıda kazanma oranı, yüzde, `p`, aralık ya da kazanan işareti
bulunmaz; round sonu da "akış bitti" demekten öte bir şey söylemez. Plant sonrası simüle edilmez.

İçeride düello sonucunu çekmek için onaylı model (`simulate.duel_p`) kullanılır; olasılık
dışarıya verilmez ve `Replay` yapısında alanı yoktur.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from squadmind.analysis import simulate as sm

MAP = "de_dust2"
MAX_WAYPOINTS = 12          # bir rotadaki en çok ara nokta
MAX_COORD = 6000.0          # Dust2 koordinatları bunun çok içinde
CLASSES = ("rifle", "pistol", "awp")
# T doğma noktası (maps.BOARD_PLACES "T Spawn" çevresi); beş jeton yan yana
T_SPAWN = (-640.0, -830.0)
T_SPAWN_OFFSETS = ((-160.0, 0.0), (-80.0, 0.0), (0.0, 0.0), (80.0, 0.0), (160.0, 0.0))


class RouteError(ValueError):
    """Rota navmesh'te kurulamadı (nokta yürünemez alanda ya da yol yok)."""


# --- Navmesh'e yapıştırma -------------------------------------------------------------------------


def snap(map_name: str, x: float, y: float, hint_z: float | None = None) -> tuple[float, float, float]:
    """(x, y) noktasını yürünebilir alana yapıştır → (x, y, z).

    Nokta bir alanın içindeyse olduğu gibi kalır (üst üste katlarda `hint_z`ya en yakın kat);
    değilse yatayda en yakın alan merkezine kayar. Tutarlı rota için z hep navmesh'ten gelir.
    """
    from squadmind.analysis import geometry as geo
    from squadmind.viz.board import _centroids

    zs = geo.floor_zs(map_name, x, y)
    if zs:
        z = min(zs, key=lambda v: abs(v - (hint_z if hint_z is not None else zs[0])))
        return (x, y, float(z))
    cands = _centroids(map_name)
    if not cands:
        raise RouteError("Harita geometrisi yok.")
    _, cx, cy, cz = min(cands, key=lambda c: (c[1] - x) ** 2 + (c[2] - y) ** 2)
    return (float(cx), float(cy), float(cz))


def resolve_route(map_name: str, start: tuple[float, float], waypoints: list[tuple[float, float]]
                  ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Başlangıç + tıklanan noktalar → (yapışmış ara noktalar, navmesh yürüme yolu).

    Yol `viz.board._route` ile (en kısa yol + string-pulling, duvara girmez). Bir bacak için
    navmesh'te yol yoksa `RouteError`.
    """
    from squadmind.viz.board import _area_at, _nav, _route

    pts: list[tuple[float, float, float]] = []
    for x, y in [start, *waypoints]:
        pts.append(snap(map_name, x, y, pts[-1][2] if pts else 0.0))
    nav = _nav(map_name)
    for a, b in zip(pts, pts[1:], strict=False):
        fa, fb = _area_at(map_name, a), _area_at(map_name, b)
        if fa is None or fb is None or (fa != fb and not nav.find_path(fa, fb, weight="distance")):
            raise RouteError("Bu noktaya yol yok.")
    path = _route(map_name, pts) if len(pts) > 1 else [(pts[0][0], pts[0][1])]
    return [(p[0], p[1]) for p in pts[1:]], path


# --- Senaryo --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """Bir oyuncunun kurulumu. `route` T için ara noktalar, CT için tek tutma noktası."""

    name: str
    elo: float
    cls: str
    route: tuple[tuple[float, float], ...]
    delay: float = 0.0
    plant: bool = False


def build_scenario(t_slots: list[Slot], ct_slots: list[Slot], map_name: str = MAP
                   ) -> tuple[sm.Scenario, list[list[tuple[float, float]]]]:
    """Slotlardan `simulate.Scenario` + her oyuncunun yapışmış yürüme yolu (önizleme için)."""
    t_units, ct_units, paths = [], [], []
    for i, s in enumerate(t_slots):
        sx, sy = T_SPAWN[0] + T_SPAWN_OFFSETS[i % 5][0], T_SPAWN[1] + T_SPAWN_OFFSETS[i % 5][1]
        _, path = resolve_route(map_name, (sx, sy), list(s.route))
        paths.append(path)
        t_units.append(sm.Unit(s.name, s.elo, sm.walk(path, start_delay=s.delay), s.cls, plant=s.plant))
    for s in ct_slots:
        z = snap(map_name, *s.route[0])
        paths.append([(z[0], z[1])])
        ct_units.append(sm.Unit(s.name, s.elo, sm.walk([(z[0], z[1])]), s.cls))
    longest = max(u.track.end + (sm.PLANT_SECONDS if u.plant else 0.0) for u in t_units)
    horizon = float(min(60.0, max(12.0, math.ceil(longest) + 3.0)))
    return sm.Scenario(map_name, tuple(t_units), tuple(ct_units), horizon=horizon), paths


# --- Önbellekler (görüş ışın izleme pahalı) -------------------------------------------------------


@lru_cache(maxsize=4)
def shared_los(map_name: str):
    """Haritanın görüş işlevi; sonuç önbelleği süreç boyunca yaşar (rotalar tekrar eder)."""
    return sm.geometry_los(map_name)


_PREP: OrderedDict[tuple, sm.Prepared] = OrderedDict()


def prepare_cached(scn: sm.Scenario) -> sm.Prepared:
    """Aynı kurulum için görüş tablosunu bir kez hesapla ("başka örnek" yalnız tohumu değiştirir)."""
    def sig(u: sm.Unit) -> tuple:
        return (round(u.elo), u.cls, u.plant, tuple(np.round(u.track.t, 2).tolist()),
                tuple(np.round(u.track.xyz[:, :2], 1).ravel().tolist()))
    key = (scn.map_name, scn.horizon, tuple(sig(u) for u in scn.t), tuple(sig(u) for u in scn.ct))
    hit = _PREP.get(key)
    if hit is None:
        hit = sm.prepare(scn, shared_los(scn.map_name))
        _PREP[key] = hit
        while len(_PREP) > 16:
            _PREP.popitem(last=False)
    else:
        _PREP.move_to_end(key)
    return hit


# --- Tek tekrar -----------------------------------------------------------------------------------


def replay(prep: sm.Prepared, seed: int = 0, params: sm.DuelParams | None = None) -> dict:
    """Tek tekrar. Dönen sözlükte olasılık / skor / kazanan alanı YOK (bkz. modül başı).

    {dt, steps, end_t, units: [{name, side, cls, xy: [[x, y] her adım], died: sn | None}],
     events: [{t, kind: "los" | "duel" | "plant", a, b}]}   (a, b = oyuncu sırası; T 0-4, CT 5-9;
    duelde a = vuran, b = vurulan; los'ta a = T, b = CT; plant'ta a = kuran)
    """
    params = params or sm.DuelParams()
    scn, rng = prep.scn, np.random.default_rng(int(seed))
    nT, nC = len(scn.t), len(scn.ct)
    alive_t, alive_c = [True] * nT, [True] * nC
    hp_t, hp_c = [100.0] * nT, [100.0] * nC
    died: dict[int, float] = {}
    events: list[dict] = []
    damage = np.asarray(params.damage, dtype=float)
    weapon = np.asarray(params.weapon)
    end_k = prep.n_steps - 1
    for k in range(prep.n_steps):
        now = k * scn.dt
        a, b = int(prep.step_ptr[k]), int(prep.step_ptr[k + 1])
        prev = set(zip(prep.pair_i[int(prep.step_ptr[k - 1]):a].tolist(),
                       prep.pair_j[int(prep.step_ptr[k - 1]):a].tolist())) if k else set()
        for idx in (a + rng.permutation(b - a)):        # çiftler her adımda rastgele sırayla
            i, j = int(prep.pair_i[idx]), int(prep.pair_j[idx])
            if not (alive_t[i] and alive_c[j]):
                continue
            where = sm.nearest_place(scn.map_name, *prep.pair_mid[idx].tolist())   # düello bölgesi (etiket)
            if (i, j) not in prev:
                events.append({"t": now, "kind": "los", "a": i, "b": nT + j, "where": where})
            p = float(sm.duel_p(prep.pair_delo[idx], prep.pair_wx[idx], hp_t[i], hp_c[j],
                                params.beta, weapon, params.hp_coef, params.base))
            t_wins = rng.random() < p
            dmg = float(damage[rng.integers(0, len(damage))]) if damage.size else 0.0
            if t_wins:
                alive_c[j], hp_t[i] = False, max(1.0, hp_t[i] - dmg)
                died[nT + j] = now
                events.append({"t": now, "kind": "duel", "a": i, "b": nT + j, "where": where})
            else:
                alive_t[i], hp_c[j] = False, max(1.0, hp_c[j] - dmg)
                died[i] = now
                events.append({"t": now, "kind": "duel", "a": nT + j, "b": i, "where": where})
        if not any(alive_c) or not any(alive_t):
            end_k = k
            break
        planted = [i for i in range(nT) if alive_t[i] and prep.plant_ready[i] <= now + 1e-9]
        if planted:
            px, py = scn.t[planted[0]].track.at(np.array([now]))[0, :2].tolist()
            events.append({"t": now, "kind": "plant", "a": planted[0], "b": -1,
                           "where": sm.nearest_place(scn.map_name, px, py)})
            end_k = k
            break
    times = np.arange(end_k + 1) * scn.dt
    units = []
    for n, (u, side) in enumerate([(u, "T") for u in scn.t] + [(u, "CT") for u in scn.ct]):
        xy = u.track.at(times)[:, :2]
        if n in died:                                    # ölen jeton olduğu yerde kalır
            xy[int(round(died[n] / scn.dt)):] = xy[int(round(died[n] / scn.dt))]
        units.append({"name": u.name, "side": side, "cls": u.cls,
                      "xy": np.round(xy, 1).tolist(), "died": died.get(n)})
    return {"dt": scn.dt, "steps": end_k + 1, "end_t": end_k * scn.dt, "units": units, "events": events}


# --- Sabit metinler ve başlangıç şablonları -------------------------------------------------------

# Ekranda her zaman görünen etiket (profesör kararı; metni değiştirmek onay ister)
FLOW_LABEL = "Örnek akış: tahmin değil, betimleme. Sınamayı geçmedi."

# Şablonlar strateji ÖNERİSİ değil, başlangıç kolaylığıdır (kaba noktalar; kullanıcı düzenler).
# Tüm T'ler aynı noktalardan geçer, son nokta yan yana dağılır (jetonlar üst üste binmesin),
# 0,35 sn arayla çıkarlar. Bomba son oyuncuda (rotanın sonunda kurar).
_TEMPLATE_WAYPOINTS = {
    "a_long": ("A long rush", ((350.0, -61.0), (815.0, 758.0), (1560.0, 1380.0), (1097.0, 2760.0))),
    "b_rush": ("B rush", ((-1700.0, 420.0), (-1900.0, 1150.0), (-1750.0, 2400.0))),
}
_END_SPREAD = ((0.0, 0.0), (-90.0, -40.0), (90.0, -40.0), (-50.0, 60.0), (50.0, 60.0))
# CT varsayılan tutma noktaları (A site, Long A, Mid Doors, B Doors, B site; kaba)
DEFAULT_CT_HOLDS = ((1097.0, 2760.0), (1560.0, 1380.0), (-515.0, 2050.0), (-1102.0, 2200.0), (-1750.0, 2400.0))


def templates() -> list[dict]:
    """Arayüzdeki hazır şablonlar: her T için ara noktalar + gecikme; son oyuncu bomba taşır."""
    out = []
    for tid, (name, wps) in _TEMPLATE_WAYPOINTS.items():
        routes = []
        for i in range(5):
            pts = [list(p) for p in wps[:-1]] + [[wps[-1][0] + _END_SPREAD[i][0], wps[-1][1] + _END_SPREAD[i][1]]]
            routes.append({"points": pts, "delay": round(0.35 * i, 2)})
        out.append({"id": tid, "name": name, "routes": routes, "planter": 4})
    return out


def spawns(map_name: str = MAP) -> list[tuple[float, float]]:
    """Beş T jetonunun (yapışmış) başlangıç noktası."""
    pts = []
    for dx, dy in T_SPAWN_OFFSETS:
        x, y, _ = snap(map_name, T_SPAWN[0] + dx, T_SPAWN[1] + dy)
        pts.append((x, y))
    return pts


def default_ct_holds(map_name: str = MAP) -> list[tuple[float, float]]:
    """Beş CT'nin başlangıç tutma noktası (yapışmış)."""
    return [snap(map_name, x, y)[:2] for x, y in DEFAULT_CT_HOLDS]
