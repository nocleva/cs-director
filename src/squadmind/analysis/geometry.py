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
