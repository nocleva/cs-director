"""SquadMind görsel seti (v3): logo, seviye rozetleri 1-10 ve 8 avatar için SVG üretir.

Kullanım: python scripts/gorsel_uret.py   ->  src/squadmind/web/static/img/*.svg

Kaynak: assets/gelen/referans/*.jpg yalnızca STİL referansıdır (siteye konmaz); burada elle vektör çizildi:
kalın koyu kontur, açılı kesimler, yarım ton noktaları, turuncu / mor / turkuaz kenar ışığı.
FACEIT / Valve logosu, oyun karakteri ve gerçek silah YOK; rozet rengi yalnız seviye bandı rengidir.
"""
from __future__ import annotations

import math
from pathlib import Path

HEDEF = Path(__file__).resolve().parent.parent / "src" / "squadmind" / "web" / "static" / "img"
INK = "#0B0E18"          # kalın kontur
PLUM = "#170F2E"         # logo konturu (referanstaki mor-siyah)
ORANGE, VIOLET, CYAN = "#FF6A1F", "#8B6CFF", "#38E1D0"
SVGH = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" role="img" aria-label="{al}">'


def yaz(ad: str, icerik: str) -> None:
    (HEDEF / ad).write_text(icerik, encoding="utf-8")


def pts(p: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:g},{y:g}" for x, y in p)


def karistir(a: str, b: str, t: float) -> str:
    """İki #RRGGBB rengi t oranında karıştırır (0 = a, 1 = b)."""
    ra, rb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ra, rb))


# ======================================================================= LOGO
# Referans (turuncu chevron + mor çapraz bant + mor kancalar + turuncu kuyruk) 1000x1110 kutuda yeniden çizildi.
O1 = [(966, 478), (1192, 724), (1112, 812), (966, 662), (692, 970), (690, 988), (546, 1092), (530, 940)]   # turuncu chevron
SLASH = [(508, 1265), (1398, 528), (1398, 693), (508, 1437)]                                              # mor çapraz bant
HOOK = [(966, 735), (1131, 900), (1050, 969), (966, 885), (741, 1110), (591, 1110)]                              # mor iç chevron (^)
VBOT = [(791, 1040), (966, 1215), (1311, 870), (1161, 870), (966, 1065), (941, 1040)]                           # mor alt v
BLOCK = [(1240, 860), (1330, 855), (1400, 930), (1400, 1020), (1330, 1090), (1240, 960)]                  # mor sağ blok
TAIL = [(1190, 1008), (1425, 1255), (1425, 1437), (1118, 1092)]                                          # turuncu kuyruk


def logo_svg(detay: bool) -> str:
    vb = "455 415 1020 1130"
    o = [SVGH.format(vb=vb, al="SquadMind"), "<defs>"]
    if detay:
        # yarım ton: nokta deseni, orta kısımda yoğun, kenarlara doğru siliniyor
        o.append('<pattern id="ht" width="22" height="22" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><circle cx="11" cy="11" r="4.2" fill="#000" fill-opacity=".22"/></pattern>')
        o.append('<linearGradient id="go" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FF8A2B"/><stop offset="1" stop-color="#E5461A"/></linearGradient>')
        o.append('<linearGradient id="gv" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#9B4DFF"/><stop offset="1" stop-color="#4B22C8"/></linearGradient>')
        fo, fv = "url(#go)", "url(#gv)"
    else:
        fo, fv = ORANGE, VIOLET
    o.append("</defs>")
    sekiller = [(O1, fo), (TAIL, fo), (VBOT, fv), (HOOK, "#B58CFF"), (SLASH, fv)]
    if not detay:
        sekiller = [(O1, fo), (TAIL, fo), (HOOK, "#C9AEFF"), (SLASH, fv)]
    # 1. geçiş: hepsinin altına kalın koyu kontur (ortak silüet)
    o.append(f'<g fill="{PLUM}" stroke="{PLUM}" stroke-width="{70 if not detay else 62}" stroke-linejoin="miter" stroke-miterlimit="3">')
    o += [f'<polygon points="{pts(p)}"/>' for p, _ in sekiller]
    o.append("</g>")
    # 2. geçiş: dolgular; her biri ince açık kenar + koyu ayırıcı çizgi
    for p, f in sekiller:
        kenar = "#FFB36B" if f in (fo, "url(#go)") else "#C58CFF"
        o.append(f'<polygon points="{pts(p)}" fill="{f}" stroke="{PLUM}" stroke-width="22" stroke-linejoin="miter" paint-order="stroke"/>')
        o.append(f'<polygon points="{pts(p)}" fill="none" stroke="{kenar}" stroke-opacity=".55" stroke-width="5" stroke-linejoin="miter"/>')
        if detay:
            o.append(f'<polygon points="{pts(p)}" fill="url(#ht)"/>')
    o.append("</svg>")
    return "".join(o)


# ======================================================================= ROZET
# Seviye bandı renkleri (FACEIT'in kendi logosu değil; _kart.html LV_COLOR ile aynı)
LV_COLOR = {1: "#CFCFCF", 2: "#1CE400", 3: "#1CE400", 4: "#FFC800", 5: "#FFC800", 6: "#FFC800", 7: "#FFC800",
            8: "#FF6309", 9: "#FF6309", 10: "#FE1F00"}
# Basit blok rakamlar (10 x 16 kutu): font gerektirmez, her boyutta keskin
RAKAM = {
    "0": "M0,0H10V16H0Z", "1": "M0,4L5,0V16", "2": "M0,0H10V8H0V16H10", "3": "M0,0H10V16H0M3,8H10",
    "4": "M0,0V8H10M10,0V16", "5": "M10,0H0V8H10V16H0", "6": "M10,0H0V16H10V8H0", "7": "M0,0H10L4,16",
    "8": "M0,0H10V16H0ZM0,8H10", "9": "M10,8H0V0H10V16H0",
}


def hex_pts(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """Sivri tepeli altıgen köşeleri."""
    return [(cx + r * math.sin(math.radians(60 * i)), cy - r * math.cos(math.radians(60 * i))) for i in range(6)]


def kanat(yon: int, cx: float, cy: float, boy: int, ana: str, acik: str, koyu: str) -> str:
    """Bir tarafın kanadı (yon = -1 sol, +1 sağ): yukarı kıvrık sivri tüyler."""
    o = []
    for i in range(boy):                                  # yelpaze: her tüy yaprak biçimli, renk dönüşümlü
        a = math.radians(4 + i * (62 / max(boy - 1, 1)))  # yatayın üstündeki açı
        L, w = 27 - i * 1.0, 6.4
        tx, ty = cx + yon * L * math.cos(a), cy + 6 - L * math.sin(a)
        mx, my = cx + yon * L * .45 * math.cos(a), cy + 6 - L * .45 * math.sin(a)
        nx, ny = -math.sin(a) * w * yon, -math.cos(a) * w
        p = [(cx, cy + 6), (mx + nx, my + ny), (tx, ty), (mx - nx, my - ny)]
        c = koyu if i % 2 == 0 else ana
        o.append(f'<polygon points="{pts(p)}" fill="{c}" stroke="{INK}" stroke-width="2" stroke-linejoin="round"/>')
        o.append(f'<path d="M{cx + yon * 4 * math.cos(a):g},{cy + 6 - 4 * math.sin(a):g}L{mx + (tx - mx) * .8:g},{my + (ty - my) * .8:g}" stroke="{acik}" stroke-width="1.1" stroke-opacity=".85" fill="none"/>')
    return "".join(o)


def rozet_svg(n: int) -> str:
    ana = LV_COLOR[n]
    acik, koyu = karistir(ana, "#FFFFFF", .42), karistir(ana, "#000000", .45)
    cy = 54 if n >= 8 else 50
    R = {10: 30, 9: 30, 8: 30}.get(n, 38)
    o = [SVGH.format(vb="0 0 100 100", al=f"Seviye {n}"), "<defs>"]
    o.append(f'<linearGradient id="g{n}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{karistir(ana, "#FFFFFF", .2)}"/><stop offset="1" stop-color="{karistir(ana, "#000000", .25)}"/></linearGradient>')
    if n == 10:
        o.append('<radialGradient id="ates"><stop offset=".55" stop-color="#FF8A1F" stop-opacity=".65"/><stop offset="1" stop-color="#FE1F00" stop-opacity="0"/></radialGradient>')
    if n >= 4:
        o.append('<pattern id="ht" width="3.2" height="3.2" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><circle cx="1.6" cy="1.6" r=".62" fill="#000" fill-opacity=".2"/></pattern>')
    o.append("</defs>")
    if n == 10:
        o.append(f'<circle cx="50" cy="{cy}" r="47" fill="url(#ates)"/>')
        for dx, dy, r in [(12, 20, 1.3), (88, 26, 1.1), (8, 66, 1), (92, 70, 1.2), (24, 10, .9), (77, 9, .9)]:
            o.append(f'<circle cx="{dx}" cy="{dy}" r="{r}" fill="#FFB347"/>')
    # kanatlar (8+): 8 üç tüy, 9 dört, 10 beş tüy
    if n >= 8:
        boy = {8: 3, 9: 4, 10: 5}[n]
        o.append(kanat(-1, 50 - R * .78, cy, boy, ana, acik, "#4A5266" if n == 10 else koyu))
        o.append(kanat(1, 50 + R * .78, cy, boy, ana, acik, "#4A5266" if n == 10 else koyu))
    # seviye 10: dikenli halka ve altın dikenler
    if n == 10:
        o.append(f'<circle cx="50" cy="{cy}" r="{R + 4}" fill="none" stroke="{INK}" stroke-width="5.5"/><circle cx="50" cy="{cy}" r="{R + 4}" fill="none" stroke="#6B7285" stroke-width="2.6"/>')
        for a in (90, 150, 210, 270, 330, 30):
            r0, r1 = R + 3, R + 12
            x0, y0 = 50 + r0 * math.cos(math.radians(a)), cy + r0 * math.sin(math.radians(a))
            x1, y1 = 50 + r1 * math.cos(math.radians(a)), cy + r1 * math.sin(math.radians(a))
            nx, ny = -math.sin(math.radians(a)) * 3, math.cos(math.radians(a)) * 3
            o.append(f'<polygon points="{pts([(x0 + nx, y0 + ny), (x1, y1), (x0 - nx, y0 - ny)])}" fill="#FFC13A" stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/>')
    # 4+: küçük çentik / diken (köşelerde kısa üçgenler)
    if 4 <= n <= 9:
        k = 3 + (n - 4)                                    # 4: iki köşe ... 7: daha çok vurgu
        for i, (vx, vy) in enumerate(hex_pts(50, cy, R + 1)):
            if n < 8 and i not in ((0, 3) if n == 4 else (0, 2, 4) if n == 5 else (0, 1, 3, 5) if n == 6 else (0, 1, 2, 3, 4, 5)):
                continue
            a = math.radians(60 * i - 90)
            tx, ty = vx + 5.5 * math.cos(a), vy + 5.5 * math.sin(a)
            nx, ny = -math.sin(a) * 3.2, math.cos(a) * 3.2
            o.append(f'<polygon points="{pts([(vx + nx, vy + ny), (tx, ty), (vx - nx, vy - ny)])}" fill="{acik}" stroke="{INK}" stroke-width="1.8" stroke-linejoin="round"/>')
    # gövde: koyu kontur, ana renk altıgen, parlak kenar, iç koyu altıgen, orta sırt (sol açık / sağ koyu yüz)
    o.append(f'<polygon points="{pts(hex_pts(50, cy, R + 2.6))}" fill="{INK}" stroke="{INK}" stroke-width="2.4" stroke-linejoin="round"/>')
    o.append(f'<polygon points="{pts(hex_pts(50, cy, R))}" fill="url(#g{n})" stroke="{acik}" stroke-width="1.6" stroke-linejoin="round"/>')
    ic = hex_pts(50, cy, R * .74)
    o.append(f'<polygon points="{pts(ic)}" fill="{INK}" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>')
    o.append(f'<polygon points="{pts(hex_pts(50, cy, R * .70))}" fill="{karistir(ana, "#000000", .72)}" stroke="{ana}" stroke-width="1.5" stroke-linejoin="round"/>')
    # sırt: sol yarı hafif aydınlık (facet); n>=4 yarım ton
    sol = [hex_pts(50, cy, R * .70)[i] for i in (0, 5, 4, 3)]
    o.append(f'<polygon points="{pts(sol)}" fill="#FFFFFF" fill-opacity=".07"/>')
    if n >= 4:
        o.append(f'<polygon points="{pts(hex_pts(50, cy, R))}" fill="url(#ht)"/>')
    # taç (10)
    if n == 10:
        ty = cy - R - 2
        taç = [(40, ty + 3), (41, ty - 8), (45, ty - 2), (50, ty - 11), (55, ty - 2), (59, ty - 8), (60, ty + 3)]
        o.append(f'<polygon points="{pts(taç)}" fill="{ana}" stroke="{INK}" stroke-width="2.2" stroke-linejoin="round"/>')
        o.append(f'<polygon points="{pts([(50, ty - 5), (52, ty - 1), (50, ty + 2), (48, ty - 1)])}" fill="#FFC13A"/>')
    # rakam: krem renk, kalın koyu kontur (blok yazı); iki haneliyse yan yana
    s = str(n)
    yuk = R * .70 * 0.95 * (0.72 if len(s) == 1 else 0.56)     # rakam yüksekliği
    sc = yuk / 16
    gen = 10 * sc
    ara = 4 * sc
    toplam = len(s) * gen + (len(s) - 1) * ara
    x0 = 50 - toplam / 2
    y0 = cy - yuk / 2
    gl = []
    for i, ch in enumerate(s):
        gl.append(f'<path transform="translate({x0 + i * (gen + ara):.2f},{y0:.2f}) scale({sc:.4f})" d="{RAKAM[ch]}"/>')
    kal = (5.0 if len(s) == 1 else 5.4) / sc            # çizgi kalınlığı: yol ölçeği sc ile bölünür
    o.append(f'<g fill="none" stroke="{INK}" stroke-linejoin="miter" stroke-linecap="square" stroke-width="{kal * 2.0:.2f}">{"".join(gl)}</g>')
    o.append(f'<g fill="none" stroke="#FFF6E0" stroke-linejoin="miter" stroke-linecap="square" stroke-width="{kal:.2f}">{"".join(gl)}</g>')
    o.append("</svg>")
    return "".join(o)


# ======================================================================= AVATAR
AVATARLAR = [  # (desen, renk1, renk2)
    ("sunburst", ORANGE, CYAN), ("stripes", VIOLET, ORANGE), ("triangles", CYAN, VIOLET), ("dots", "#FF5C62", "#F5C542"),
    ("rings", "#3FD07A", CYAN), ("waves", "#F5C542", "#FF5C62"), ("grid", VIOLET, CYAN), ("chevrons", "#FF8A3D", VIOLET),
]


def desen(ad: str, c1: str, c2: str) -> str:
    """Altıgenin içindeki arka plan deseni (100x100 kutu)."""
    o = []
    if ad == "sunburst":
        for i in range(18):
            a0, a1 = math.radians(i * 20 - 4), math.radians(i * 20 + 4 + (6 if i % 2 else 0))
            c = c1 if i % 2 == 0 else c2
            o.append(f'<polygon points="50,46 {50 + 90 * math.cos(a0):.1f},{46 + 90 * math.sin(a0):.1f} {50 + 90 * math.cos(a1):.1f},{46 + 90 * math.sin(a1):.1f}" fill="{c}"/>')
    elif ad == "stripes":
        for i in range(-3, 9):
            c = c1 if i % 2 == 0 else c2
            o.append(f'<polygon points="{i * 16 - 10},100 {i * 16 + 8},100 {i * 16 + 68},0 {i * 16 + 50},0" fill="{c}"/>')
    elif ad == "triangles":
        for r in range(0, 8):
            for q in range(-1, 9):
                x, y = q * 16 + (8 if r % 2 else 0), r * 14
                c = c1 if (r + q) % 3 else c2
                o.append(f'<polygon points="{x},{y + 14} {x + 8},{y} {x + 16},{y + 14}" fill="{c}" fill-opacity="{.95 if (r + q) % 2 else .55}"/>')
    elif ad == "dots":
        for r in range(0, 9):
            for q in range(0, 9):
                x, y = q * 13 + (6 if r % 2 else 0), r * 12 + 4
                o.append(f'<circle cx="{x}" cy="{y}" r="{4.6 - r * .12 if (r + q) % 2 else 3:.1f}" fill="{c1 if (r + q) % 2 else c2}"/>')
    elif ad == "rings":
        for i, r in enumerate((70, 58, 46, 34, 22)):
            o.append(f'<circle cx="50" cy="46" r="{r}" fill="{c1 if i % 2 == 0 else c2}"/>')
    elif ad == "waves":
        for i in range(0, 9):
            y = i * 13 - 4
            c = c1 if i % 2 == 0 else c2
            o.append(f'<path d="M-4,{y} Q10,{y - 9} 25,{y} T55,{y} T85,{y} T115,{y} V{y + 14} H-4Z" fill="{c}"/>')
    elif ad == "grid":
        o.append(f'<rect width="100" height="100" fill="{c1}" fill-opacity=".35"/>')
        for i in range(0, 11):
            o.append(f'<path d="M{i * 10},0V100M0,{i * 10}H100" stroke="{c2 if i % 3 == 0 else c1}" stroke-width="{2.6 if i % 3 == 0 else 1.4}"/>')
        for x, y in ((30, 30), (70, 20), (80, 60), (20, 70)):
            o.append(f'<rect x="{x - 4}" y="{y - 4}" width="8" height="8" fill="{c2}"/>')
    elif ad == "chevrons":
        for i in range(0, 9):
            y = i * 14 - 10
            o.append(f'<polygon points="-5,{y + 14} 50,{y - 14} 105,{y + 14} 105,{y + 24} 50,{y - 4} -5,{y + 24}" fill="{c1 if i % 2 == 0 else c2}"/>')
    return "".join(o)


def avatar_svg(i: int) -> str:
    ad, c1, c2 = AVATARLAR[i - 1]
    H = hex_pts(50, 50, 47)
    o = [SVGH.format(vb="0 0 100 100", al=f"Avatar {i}"), "<defs>",
         f'<clipPath id="k{i}"><polygon points="{pts(H)}"/></clipPath>',
         f'<radialGradient id="v{i}"><stop offset="0" stop-color="{c2}"/><stop offset="1" stop-color="{c2}" stop-opacity="0"/></radialGradient>', "</defs>"]
    # HUD köşe çizgileri (altıgenin dışında, kutu köşelerinde)
    for sx, sy in ((1, 1), (99, 1), (1, 99), (99, 99)):
        dx, dy = (4 if sx < 50 else -4), (4 if sy < 50 else -4)
        o.append(f'<path d="M{sx},{sy + dy * 1.6:g}V{sy}H{sx + dx * 1.6:g}" fill="none" stroke="{c2}" stroke-width="1.6" stroke-opacity=".75"/>')
    # dış kontur + zemin
    o.append(f'<polygon points="{pts(H)}" fill="{INK}" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>')
    o.append(f'<g clip-path="url(#k{i})"><rect width="100" height="100" fill="#10152A"/>{desen(ad, c1, c2)}')
    o.append(f'<rect width="100" height="100" fill="{INK}" fill-opacity=".34"/>')           # deseni kısarak miğferi öne çıkar
    # omuz zırhı
    o.append(f'<polygon points="{pts([(8, 100), (14, 86), (32, 76), (44, 72), (56, 72), (68, 76), (86, 86), (92, 100)])}" fill="#1B2133" stroke="{INK}" stroke-width="2.6" stroke-linejoin="round"/>')
    o.append(f'<polygon points="{pts([(14, 86), (32, 76), (36, 82), (20, 92)])}" fill="{c1}"/><polygon points="{pts([(86, 86), (68, 76), (64, 82), (80, 92)])}" fill="{c1}"/>')
    o.append(f'<polygon points="{pts([(42, 100), (44, 86), (56, 86), (58, 100)])}" fill="{INK}"/><polygon points="{pts([(46, 92), (50, 88), (54, 92), (50, 96)])}" fill="{c1}"/>')
    # miğfer
    mig = [(35, 24), (50, 19), (65, 24), (72, 34), (72, 54), (66, 68), (58, 74), (42, 74), (34, 68), (28, 54), (28, 34)]
    o.append(f'<polygon points="{pts(mig)}" fill="#1D2540" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>')
    o.append(f'<polygon points="{pts([(38, 26), (50, 22), (62, 26), (60, 38), (40, 38)])}" fill="#2B3556" stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/>')
    o.append(f'<path d="M50,22V38" stroke="{c1}" stroke-width="2.2"/><path d="M28,46L26,56L30,64M72,46L74,56L70,64" stroke="{c1}" stroke-width="2.4" fill="none" stroke-linejoin="round"/>')
    o.append(f'<polygon points="{pts([(42, 66), (58, 66), (56, 73), (44, 73)])}" fill="{c1}" stroke="{INK}" stroke-width="1.4"/>')
    # parlayan vizör
    viz = [(31, 46), (69, 46), (65, 56), (57, 54), (43, 54), (35, 56)]
    o.append(f'<ellipse cx="50" cy="51" rx="30" ry="12" fill="url(#v{i})" fill-opacity=".55"/>')
    o.append(f'<polygon points="{pts(viz)}" fill="{c2}" stroke="{INK}" stroke-width="1.8" stroke-linejoin="round"/>')
    o.append(f'<polygon points="{pts([(35, 48), (62, 48), (60, 51), (36, 51)])}" fill="#FFFFFF" fill-opacity=".7"/>')
    o.append("</g>")
    # çerçeve: renk 1 dış, renk 2 ince iç ışık
    o.append(f'<polygon points="{pts(H)}" fill="none" stroke="{c1}" stroke-width="3.4" stroke-linejoin="round"/>')
    o.append(f'<polygon points="{pts(hex_pts(50, 50, 43.2))}" fill="none" stroke="{INK}" stroke-width="1.4" stroke-linejoin="round"/>')
    o.append(f'<path d="M{pts([H[5]]).replace(" ", ",")}L{pts([H[0]]).replace(" ", ",")}L{pts([H[1]]).replace(" ", ",")}" fill="none" stroke="{c2}" stroke-width="1.2" stroke-opacity=".9" transform="translate(0,2.6) scale(1)"/>')
    o.append("</svg>")
    return "".join(o)


def main() -> None:
    HEDEF.mkdir(parents=True, exist_ok=True)
    yaz("logo.svg", logo_svg(True))
    yaz("logo-mark.svg", logo_svg(False))
    for n in range(1, 11):
        yaz(f"rozet-{n}.svg", rozet_svg(n))
    for i in range(1, 9):
        yaz(f"avatar-{i}.svg", avatar_svg(i))
    print("logo.svg, logo-mark.svg, rozet-1..10.svg, avatar-1..8.svg yazıldı:", HEDEF)


if __name__ == "__main__":
    main()
