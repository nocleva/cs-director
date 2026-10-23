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


# Taktik tahtasında yazılan bölge adları: (x, y) dünya koordinatı, etiketin ortası
BOARD_PLACES: dict[str, dict[str, tuple[float, float]]] = {
    "de_dust2": {
        "T Spawn": (-640, -830), "Outside Long": (350, -61), "Long Doors": (815, 758),
        "Pit": (1450, 520), "Long A": (1560, 1380), "A Site": (1097, 2760), "Short": (300, 1560),
        "Catwalk": (-120, 1180), "Mid": (-420, 900), "Xbox": (-300, 1520), "Top Mid": (-313, 260),
        "Mid Doors": (-515, 2050), "CT Spawn": (154, 2400), "Upper Tunnel": (-1900, 1150),
        "Lower": (-900, 1500), "B Site": (-1750, 2400), "B Doors": (-1102, 2200),
        "Tunnels": (-1700, 420),
    },
}


def zone_of(map_name: str | None, place: str | None) -> str | None:
    """Bölge adının alanı ('A' / 'B' / 'Mid'); bilinmiyorsa None."""
    if not map_name or not place:
        return None
    return ZONES.get(map_name, {}).get(place)


def has_zones(map_name: str | None) -> bool:
    return bool(map_name) and map_name in ZONES


# Peek hatları: en çok düello yaşanan bölge çiftleri (18 Dust2 demosundaki 2.911 kill'den,
# kullanıcıyla 23.10.2026'da onaylandı). Her hat iki bölge kümesi; bir kill, saldıranla kurbanın
# bölgeleri iki kümeye (hangi yönde olursa olsun) düşüyorsa o hatta sayılır. Sıra önemli: bir
# çift birden çok hatta uyarsa ilk hat alınır.
PEEK_LINES: dict[str, list[tuple[str, str, frozenset[str], frozenset[str]]]] = {
    "de_dust2": [
        ("long", "Long Doors ↔ Long A", frozenset({"LongDoors"}), frozenset({"LongA"})),
        ("mid_awp", "Top Mid ↔ Mid Doors", frozenset({"TopofMid"}), frozenset({"MidDoors"})),
        ("b_site", "B Site / Upper Tunnel ↔ B Site", frozenset({"BombsiteB", "UpperTunnel"}),
         frozenset({"BombsiteB"})),
        ("mid_doors", "Mid Doors yakın temas", frozenset({"MidDoors"}), frozenset({"MidDoors"})),
        ("long_corner", "Long Doors köşe teması", frozenset({"LongDoors"}), frozenset({"LongDoors"})),
        ("a_long", "Long A ↔ A Site / Under A / Ramp", frozenset({"LongA"}),
         frozenset({"BombsiteA", "UnderA", "ARamp", "LongA"})),
        ("cat_short", "Top Mid / Catwalk ↔ Catwalk / Short Stairs", frozenset({"TopofMid", "Catwalk"}),
         frozenset({"Catwalk", "ShortStairs"})),
        ("ext_a", "Extended A ↔ Under A / A Ramp", frozenset({"ExtendedA"}), frozenset({"UnderA", "ARamp"})),
        ("ct_mid", "Mid Doors ↔ CT Spawn", frozenset({"MidDoors"}), frozenset({"CTSpawn"})),
        ("b_doors", "B Doors ↔ B Site", frozenset({"BDoors"}), frozenset({"BombsiteB"})),
    ],
}

# T tarafının site'lara yaklaşım koridorları (taktik desen tespiti için).
# Split = site'a en az iki farklı koridordan girmek (A: long + short, B: tünel + mid).
LANES: dict[str, dict[str, str]] = {
    "de_dust2": {
        "OutsideLong": "long", "LongDoors": "long", "Pit": "long", "Side": "long",
        "LongA": "long",
        "Catwalk": "short", "ShortStairs": "short",
        "TopofMid": "mid", "Middle": "mid", "MidDoors": "mid", "LowerTunnel": "mid",
        "CTSpawn": "mid", "BDoors": "mid",
        "OutsideTunnel": "tunnels", "UpperTunnel": "tunnels", "TunnelStairs": "tunnels",
        "Hole": "tunnels",
    },
}

# Site'a "girildi" sayılan bölgeler: koridorun son parçası + site. Plant edilen 184
# round'da plant öncesi 10 saniyedeki T konumlarından seçildi (Outside Long, Upper
# Tunnel gibi bekleme noktaları dahil değil).
HIT_AREAS: dict[str, dict[str, str]] = {
    "de_dust2": {
        "LongA": "A", "ARamp": "A", "BombsiteA": "A", "ExtendedA": "A", "UnderA": "A",
        "ShortStairs": "A",
        "BombsiteB": "B", "BDoors": "B", "Hole": "B",
    },
}
