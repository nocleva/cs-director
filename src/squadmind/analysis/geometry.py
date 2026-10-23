"""Görüş hattı: iki nokta birbirini görüyor mu (haritanın 3B geometrisiyle).

awpy'nin `VisibilityChecker`'ı haritanın üçgen modeliyle (Dust2: ~240 bin üçgen) ışın atar.
Doğrulama (23.10.2026, 300 Dust2 kill'i): duvarsız kill'lerin %94'ünde saldıran ve kurban
birbirini görüyor çıktı, duvar arkası (wallbang) kill'lerin 9/14'ünde görmüyor. Taktik
oluşturucuda iki oyuncu arasında düello hesaplamadan önce bakılır: görmüyorlarsa düello yok.
"""

from __future__ import annotations

from functools import lru_cache

from awpy._awpy import VisibilityChecker

# Göz yüksekliği (ayakta, oyun birimi): konumlar ayak hizasında kayıtlı
EYE_HEIGHT = 64.0


@lru_cache(maxsize=4)
def _checker(map_name: str) -> VisibilityChecker:
    return VisibilityChecker(map_name)


def can_see(map_name: str, a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
    """a ve b (ayak konumu x, y, z) ayakta birbirini görüyor mu."""
    eye_a = (a[0], a[1], a[2] + EYE_HEIGHT)
    eye_b = (b[0], b[1], b[2] + EYE_HEIGHT)
    return bool(_checker(map_name).is_visible(eye_a, eye_b))


# --- Arena: yükseklik bilinmeyen (x, y) noktaları için görüş --------------------------------
# Arenada oyuncu haritaya (x, y) olarak bırakılır, yükseklik yok. Eski yol: en yakın bölge
# hücresinin ortalama z'si. Doğrulama (900 duvarsız Dust2 kill'i, gerçek konumlarla): gerçek z
# ile %93,1 "görüyor"; hücre z'si ile %83,7 (88 yanlış "görmüyor"; z hatası >48 olan çiftlerde
# %75 yanlış). Navmesh katları: yanlış "görmüyor" 88 → 28, yanlış "görüyor" 3 → 3 (%90,3).


@lru_cache(maxsize=4)
def _nav_polys(map_name: str) -> tuple[tuple[tuple[tuple[float, float], ...], float,
                                              tuple[float, float, float, float]], ...]:
    """Navmesh alanları: (köşeler (x, y), ortalama z, sınır kutusu x0, x1, y0, y1)."""
    from awpy import NavMesh
    nav = NavMesh(map_name)
    out = []
    for area_id in nav.areas["area_id"].to_list():
        area = nav.area(area_id)
        if not area or len(area["corners"]) < 3:
            continue
        pts = tuple((c[0], c[1]) for c in area["corners"])
        z = sum(c[2] for c in area["corners"]) / len(area["corners"])
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        out.append((pts, z, (min(xs), max(xs), min(ys), max(ys))))
    return tuple(out)


def _inside(poly: tuple[tuple[float, float], ...], x: float, y: float) -> bool:
    """Nokta çokgenin içinde mi (ışın atma)."""
    inside = False
    for i, (x1, y1) in enumerate(poly):
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def floor_zs(map_name: str, x: float, y: float) -> list[float]:
    """(x, y) altındaki yürünebilir katların yükseklikleri (navmesh), yakın katlar birleştirilir.

    Üst üste iki kat varsa (ör. köprü / alt geçit) ikisi de döner; hiçbiri yoksa boş liste.
    """
    zs = sorted({round(z) for poly, z, (x0, x1, y0, y1) in _nav_polys(map_name)
                 if x0 <= x <= x1 and y0 <= y <= y1 and _inside(poly, x, y)})
    return [float(z) for z in zs]


def can_see_xy(map_name: str, a: tuple[float, float], b: tuple[float, float],
               hint_a: float = 0.0, hint_b: float = 0.0) -> bool:
    """İki (x, y) noktası görüş hattında mı: navmesh katlarından herhangi bir çift görüyorsa evet.

    Kat bulunamazsa `hint` yüksekliği (ör. bölge hücresinin z'si) kullanılır. Çok katlı noktada
    "herhangi bir çift" seçilir: yanlış "görmüyor" (düelloyu gereksiz yok sayma) azalır, yanlış
    "görüyor" artmaz (900 kill'de 3'te 3).
    """
    za = floor_zs(map_name, *a) or [hint_a]
    zb = floor_zs(map_name, *b) or [hint_b]
    return any(can_see(map_name, (a[0], a[1], x), (b[0], b[1], y)) for x in za for y in zb)
