"""Harita bilgisi: demodaki bölge adlarını (place_name) taktik alanlara gruplar.

Anchor / rotator gibi CT rolleri "oyuncu hangi site'ı tutuyor" sorusuna dayanır;
bunun için her bölge adı A, B ya da Mid alanına atanır. Spawn bölgeleri alan dışıdır.
Bölge adları 18 Dust2 demosundaki gerçek değerlerden alındı.
"""

from __future__ import annotations

ZONES: dict[str, dict[str, str]] = {
    "de_dust2": {
        # A tarafı: long, site, short
        "LongDoors": "A", "OutsideLong": "A", "Pit": "A", "Side": "A", "LongA": "A",
        "ARamp": "A", "BombsiteA": "A", "UnderA": "A", "ExtendedA": "A", "ShortStairs": "A",
        # B tarafı: tüneller, site
        "OutsideTunnel": "B", "UpperTunnel": "B", "TunnelStairs": "B", "BombsiteB": "B",
        "BDoors": "B", "Hole": "B",
        # Orta: mid, catwalk, alt tünel, CT spawn (iki site arası)
        "TopofMid": "Mid", "Middle": "Mid", "MidDoors": "Mid", "Catwalk": "Mid",
        "LowerTunnel": "Mid", "CTSpawn": "Mid",
    },
}


def zone_of(map_name: str | None, place: str | None) -> str | None:
    """Bölge adının alanı ('A' / 'B' / 'Mid'); bilinmiyorsa None."""
    if not map_name or not place:
        return None
    return ZONES.get(map_name, {}).get(place)


def has_zones(map_name: str | None) -> bool:
    return bool(map_name) and map_name in ZONES
