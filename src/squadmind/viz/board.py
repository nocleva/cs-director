"""Taktik tahtası: haritanın kuş bakışı çizimi + oyuncu rotaları, utility ve rakip spotları.

Harita, awpy'nin navmesh'inden (yürünebilir alan poligonları, gerçek geometri) çizilir:
Valve'in radar görseli kullanılmaz, kendi çizimimizdir. Yükseklik üç tonla gösterilir
(alt geçitler koyu, siteler açık). Çıktı tek bir SVG'dir (stil etiketi yok, sadece
öznitelik): web sayfasına <img> ya da satır içi konabilir.

Taktik bir `Board` tanımıdır: oyuncular (başlangıç + gidilecek noktalar; rota navmesh'te
en kısa yürüme yoluyla çizilir), utility (atan, düştüğü yer, zaman) ve spotlar (rakibin
sık durduğu noktalar). Koordinatlar oyun dünyası birimidir (Hammer), demodakiyle aynı.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from html import escape

from awpy import NavMesh

@dataclass(frozen=True)
class Theme:
    """Tahtanın renkleri ve yazı ayarları (web temasıyla aynı)."""

    bg: str
    floor_low: str            # yükseklik bantları: alt geçitler, zemin, siteler
    floor_mid: str
    floor_high: str
    wall: str                 # zeminin dış konturu (duvar hissi)
    target_floor: str         # hedef site'ın zemini (opak karışım: üçgen izi bırakmaz)
    label: str                # bölge adları
    text: str                 # oyuncu adları, token halkası
    t: str                    # T rotası / tokenı
    support: str              # destek kolu
    t_ink: str                # token içindeki yazı
    support_ink: str
    enemy: str
    smoke: str
    flash: str
    molly: str
    grid: str | None          # None: ızgara yok
    route_width: float
    label_font: str
    label_upper: bool         # bölge adları büyük harf mi
    mono_font: str
    wall_width: float = 7.0         # duvar konturu kalınlığı (zeminin dışına taşan yarısı görünür)
    wall_opacity: float = 1.0


# Keskin Taktik Masası: HUD hissi, ızgara, büyük harf etiketler
KESKIN = Theme(
    bg="#07090C", floor_low="#10161E", floor_mid="#161E28", floor_high="#1D2733",
    wall="#2C3846", target_floor="#2E1D16", label="#5D6977", text="#EEF1F4",
    t="#FF5A1F", support="#38D5E0", t_ink="#0A0402", support_ink="#02191B",
    enemy="#FF4D4D", smoke="#A7B3C0", flash="#FFD43B", molly="#FF7A1A",
    grid="#0E1319", route_width=2.6, label_font="Arial Narrow, Arial, sans-serif",
    label_upper=True, mono_font="Consolas, monospace",
)

# Sade (İskandinav + FACEIT): nötr griler, ızgara yok, ince çizgiler, sessiz etiketler
SADE = Theme(
    bg="#121316", floor_low="#1A1B1F", floor_mid="#202126", floor_high="#27282E",
    wall="#3A3C43", target_floor="#33251F", label="#7C7F88", text="#ECECEE",
    t="#FF6A2B", support="#6CC4BE", t_ink="#1A0C05", support_ink="#0B1F1D",
    enemy="#E5484D", smoke="#B9BCC4", flash="#F5C542", molly="#FF8A3D",
    grid=None, route_width=2.2, label_font="Segoe UI, Helvetica, Arial, sans-serif",
    label_upper=False, mono_font="Consolas, monospace",
)

# Apple yaklaşımı: saf siyah zemin, koyu mod sistem renkleri (turuncu, camgöbeği, kırmızı,
# sarı), sistem yazı tipi; harita tam ekran, arayüz üstünde yüzen yarı saydam paneller
APPLE = Theme(
    bg="#000000", floor_low="#111113", floor_mid="#18181B", floor_high="#202024",
    wall="#3A3A3C", target_floor="#2B1D0F", label="#8E8E93", text="#F5F5F7",
    t="#FF9F0A", support="#64D2FF", t_ink="#1C1000", support_ink="#00202B",
    enemy="#FF453A", smoke="#AEAEB2", flash="#FFD60A", molly="#FF9F0A",
    grid=None, route_width=2.2,
    label_font="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif",
    label_upper=False, mono_font="SF Mono, Menlo, Consolas, monospace",
)

# Taktik: kullanıcının tasarım promptundaki değişkenler (koyu gri, ince beyaz çizgi, turuncu
# bizim takım, turkuaz utility bilgisi, kırmızı rakip). Duvar: --line %70, 1,5 px görünür.
# Hedef site: --map-high üstüne --accent %8 (opak karışım: üçgen izi bırakmaz)
TAKTIK = Theme(
    bg="#111214", floor_low="#1E2023", floor_mid="#272A2E", floor_high="#33373C",
    wall="#E9EBEE", target_floor="#423C3A", label="#8B9198", text="#E9EBEE",
    t="#F37321", support="#3DB5AE", t_ink="#111214", support_ink="#111214",
    enemy="#E5484D", smoke="#E9EBEE", flash="#E9EBEE", molly="#F37321",
    grid=None, route_width=1.5, label_font="Inter, Segoe UI, Helvetica, Arial, sans-serif",
    label_upper=True, mono_font="Inter, Segoe UI, sans-serif",
    wall_width=3.0, wall_opacity=0.7,
)

# Oyuncu rolleri (renk temadan gelir); eski adlar geriye uyumluluk için
T, SUPPORT = "t", "support"
T_COLOR, SUPPORT_COLOR = T, SUPPORT

# Yükseklik bantlarının sınırları (Dust2: tüneller / mid alt kat ~-120, siteler ~+100)
Z_LOW, Z_HIGH = -60.0, 60.0
SMOKE_RADIUS = 144.0        # CS2 smoke yarıçapı (oyun birimi)


@dataclass
class Player:
    label: str                      # tokenın içindeki yazı (1-5)
    name: str                       # oyuncu adı (tokenın yanında)
    start: tuple[float, float, float]
    waypoints: list[tuple[float, float, float]]   # sırayla gidilecek noktalar
    color: str = T                  # rol: "t" ya da "support" (renk temadan gelir)
    lane: float = 0.0               # aynı yoldan giden oyuncular için yanal kaydırma (px)


@dataclass
class Utility:
    kind: str                       # smoke / flash / molly / he
    land: tuple[float, float, float]
    label: str                      # ör. "0:06 XBOX"
    thrower: tuple[float, float, float] | None = None   # atış çizgisi için
    label_dx: float = 12.0          # etiket kartının işarete göre konumu (px)
    label_dy: float = -20.0


@dataclass
class Spot:
    pos: tuple[float, float]
    share: float                    # bu noktada görülme payı (0-1), halka boyutu
    label: str = ""


@dataclass
class Board:
    map_name: str
    players: list[Player] = field(default_factory=list)
    utility: list[Utility] = field(default_factory=list)
    spots: list[Spot] = field(default_factory=list)
    places: dict[str, tuple[float, float]] = field(default_factory=dict)  # bölge etiketleri
    target_places: list[tuple[float, float]] = field(default_factory=list)  # hedef site merkezleri
    target_radius: float = 520.0


def merge_spots(spots: list[Spot], min_dist: float = 280.0, keep: int = 6) -> list[Spot]:
    """Birbirine yakın spotları birleştir: en sık olandan başla, yakınındakileri at.

    Izgara hücreleri aynı köşeyi birkaç parçaya bölebiliyor (ör. CT spawn); tahtada üst
    üste binen halkalar okunmuyordu.
    """
    kept: list[Spot] = []
    for sp in sorted(spots, key=lambda x: x.share, reverse=True):
        if all(math.dist(sp.pos, k.pos) >= min_dist for k in kept):
            kept.append(sp)
        if len(kept) >= keep:
            break
    return kept


@lru_cache(maxsize=8)
def _nav(map_name: str) -> NavMesh:
    return NavMesh(map_name)


@lru_cache(maxsize=8)
def map_polygons(map_name: str) -> tuple[tuple[tuple[tuple[float, float], ...], float], ...]:
    """Navmesh alanları: (köşeler (x, y), ortalama z)."""
    nav = _nav(map_name)
    out = []
    for area_id in nav.areas["area_id"].to_list():
        area = nav.area(area_id)
        if not area or len(area["corners"]) < 3:
            continue
        corners = tuple((c[0], c[1]) for c in area["corners"])
        z = sum(c[2] for c in area["corners"]) / len(area["corners"])
        out.append((corners, z))
    return tuple(out)


class _Projection:
    """Dünya (x, y) → SVG piksel; y ekseni ters (kuzey yukarı)."""

    def __init__(self, polys, width: int, pad: int) -> None:
        xs = [x for poly, _ in polys for x, _ in poly]
        ys = [y for poly, _ in polys for _, y in poly]
        self.minx, self.maxy = min(xs), max(ys)
        span = max(max(xs) - self.minx, self.maxy - min(ys))
        self.s = (width - 2 * pad) / span
        self.pad = pad
        self.height = int((self.maxy - min(ys)) * self.s + 2 * pad)

    def __call__(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.minx) * self.s + self.pad, (self.maxy - y) * self.s + self.pad)


@lru_cache(maxsize=8)
def _centroids(map_name: str) -> tuple[tuple[int, float, float, float], ...]:
    a = _nav(map_name).areas
    return tuple(zip(a["area_id"].to_list(), a["centroid_x"].to_list(),
                     a["centroid_y"].to_list(), a["centroid_z"].to_list(), strict=True))


def _area_at(map_name: str, p: tuple[float, float, float]) -> int | None:
    """Noktanın navmesh alanı; nokta bir kenara / duvara düşmüşse en yakın alan.

    Bölge merkezleri oyuncu konumlarının ortalaması olduğu için bazen yürünemeyen bir
    yere düşer; o zaman yatayda en yakın (ve yükseklikte 200 birim içindeki) alan alınır.
    """
    hit = _nav(map_name).find_area(p)
    if hit is not None:
        return hit
    best = min(((x - p[0]) ** 2 + (y - p[1]) ** 2, aid)
               for aid, x, y, z in _centroids(map_name) if abs(z - p[2]) < 200)         if _centroids(map_name) else None
    return best[1] if best else None


def _route(map_name: str, points: list[tuple[float, float, float]]) -> list[tuple[float, float]]:
    """Noktalar arası navmesh'te en kısa yürüme yolu (alan merkezlerinden geçen çizgi)."""
    nav = _nav(map_name)
    out: list[tuple[float, float, float]] = [points[0]]
    for a, b in zip(points, points[1:], strict=False):
        fa, fb = _area_at(map_name, a), _area_at(map_name, b)
        path = nav.find_path(fa, fb, weight="distance") if fa is not None and fb is not None else None
        for area_id in (path or [])[1:-1]:
            c = nav.area(area_id)["centroid"]
            out.append((c[0], c[1], c[2]))
        out.append(b)
    return [(x, y) for x, y, _ in _shortcut(map_name, out)]


def _walkable(map_name: str, a: tuple[float, float, float], b: tuple[float, float, float],
              step: float = 35.0) -> bool:
    """a'dan b'ye düz çizgi baştan sona yürünebilir alanda mı (duvara girmiyor mu)."""
    n = max(2, int(math.dist(a[:2], b[:2]) / step))
    nav = _nav(map_name)
    for k in range(1, n):
        t = k / n
        p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
        if nav.find_area(p) is None:
            return False
    return True


def _shortcut(map_name: str, pts: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Alan merkezleri arasındaki zikzakı düzelt: görülebilen en uzak noktaya düz git.

    Her noktadan, araya duvar girmeden düz çizgiyle ulaşılabilen en ileri noktaya atlanır
    (string-pulling); çizgi koridoru izler ama sağa sola dalgalanmaz.
    """
    if len(pts) < 3:
        return pts
    out, i = [pts[0]], 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1 and not _walkable(map_name, pts[i], pts[j]):
            j -= 1
        out.append(pts[j])
        i = j
    return out


def _simplify(pts: list[tuple[float, float]], min_step: float) -> list[tuple[float, float]]:
    """Birbirine çok yakın noktaları at (yumuşak eğri için)."""
    keep = [pts[0]]
    for p in pts[1:-1]:
        if math.dist(p, keep[-1]) >= min_step:
            keep.append(p)
    keep.append(pts[-1])
    return keep


def _offset(px: list[tuple[float, float]], lane: float) -> list[tuple[float, float]]:
    """Çizgiyi yanal olarak kaydır (paralel şeritler); uç noktalar sabit kalmaz, hepsi kayar."""
    if not lane or len(px) < 2:
        return px
    out = []
    for i, (x, y) in enumerate(px):
        a, b = px[max(i - 1, 0)], px[min(i + 1, len(px) - 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy) or 1
        out.append((x - dy / ln * lane, y + dx / ln * lane))
    return out


def _smooth_path(px: list[tuple[float, float]]) -> str:
    """Catmull-Rom → kübik Bézier: noktalardan geçen yumuşak SVG yolu."""
    if len(px) < 2:
        return ""
    d = [f"M{px[0][0]:.1f},{px[0][1]:.1f}"]
    for i in range(len(px) - 1):
        p0 = px[i - 1] if i > 0 else px[i]
        p1, p2 = px[i], px[i + 1]
        p3 = px[i + 2] if i + 2 < len(px) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def render_board(board: Board, width: int = 1000, pad: int = 28,
                 theme: Theme = KESKIN) -> str:
    """Taktik tahtasını SVG metni olarak üretir."""
    th = theme
    polys = map_polygons(board.map_name)
    P = _Projection(polys, width, pad)
    s = P.s
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{P.height}" '
           f'viewBox="0 0 {width} {P.height}" role="img" aria-label="Taktik tahtası">',
           '<defs>'
           f'<marker id="arrT" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{th.t}"/></marker>'
           f'<marker id="arrS" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{th.support}"/></marker>'
           '</defs>',
           f'<rect width="{width}" height="{P.height}" fill="{th.bg}"/>']

    # Izgara: taktik tahtası hissi (sade temada yok)
    if th.grid:
        step = 40
        out.append('<path d="' + " ".join(f"M{x},0 V{P.height}" for x in range(0, width, step))
                   + " " + " ".join(f"M0,{y} H{width}" for y in range(0, P.height, step))
                   + f'" stroke="{th.grid}" stroke-width="1"/>')

    def poly_d(poly) -> str:
        pts = [P(x, y) for x, y in poly]
        return "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts) + " Z"

    # 1) Kontur: tüm alanlar kalın duvar rengiyle (dış kenar duvar gibi görünür)
    walls = " ".join(poly_d(poly) for poly, _ in polys)
    out.append(f'<path d="{walls}" fill="{th.wall}" fill-opacity="{th.wall_opacity}" stroke="{th.wall}" '
               f'stroke-opacity="{th.wall_opacity}" stroke-width="{th.wall_width}" stroke-linejoin="round"/>')
    # 2) Zemin: yükseklik bandına göre ton; aynı renkte ince çizgi alan aralarını kapatır
    for color, cond in ((th.floor_low, lambda z: z < Z_LOW), (th.floor_mid, lambda z: Z_LOW <= z < Z_HIGH),
                        (th.floor_high, lambda z: z >= Z_HIGH)):
        d = " ".join(poly_d(poly) for poly, z in polys if cond(z))
        out.append(f'<path d="{d}" fill="{color}" stroke="{color}" stroke-width="1.6" stroke-linejoin="round"/>')
    # 3) Hedef site: zemin turuncuya boyanır. Opak karışık renk: yarı saydam boya üst üste
    #    binen alan kenarlarını (üçgenleri) görünür kılıyordu
    if board.target_places:
        r2 = board.target_radius ** 2
        d = " ".join(
            poly_d(poly) for poly, _ in polys
            if any((sum(x for x, _ in poly) / len(poly) - tx) ** 2
                   + (sum(y for _, y in poly) / len(poly) - ty) ** 2 < r2
                   for tx, ty in board.target_places))
        out.append(f'<path d="{d}" fill="{th.target_floor}" stroke="{th.target_floor}" stroke-width="1.6" '
                   'stroke-linejoin="round"/>')

    # Bölge etiketleri
    for name, (x, y) in board.places.items():
        px, py = P(x, y)
        text = name.upper() if th.label_upper else name
        weight, spacing = (700, 1.5) if th.label_upper else (600, 0.2)
        out.append(f'<text x="{px:.1f}" y="{py:.1f}" fill="{th.label}" font-family="{th.label_font}" '
                   f'font-size="12" font-weight="{weight}" letter-spacing="{spacing}" '
                   f'text-anchor="middle">{escape(text)}</text>')

    # Rakip spotları: halka büyüklüğü sıklıkla
    for sp in board.spots:
        px, py = P(*sp.pos)
        r = 6 + 22 * math.sqrt(sp.share)
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.1f}" fill="{th.enemy}" fill-opacity="0.10" '
                   f'stroke="{th.enemy}" stroke-width="1.5" stroke-dasharray="3 3"/>')
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{th.enemy}"/>')
        if sp.label:
            out.append(f'<text x="{px + r + 4:.1f}" y="{py + 4:.1f}" fill="{th.enemy}" font-family="{th.mono_font}" '
                       f'font-size="11" font-weight="700">{escape(sp.label)}</text>')

    # Utility: atış çizgisi (ince, noktalı yay) + düştüğü yerdeki işaret
    for u in board.utility:
        lx, ly = P(u.land[0], u.land[1])
        color = {"smoke": th.smoke, "flash": th.flash, "molly": th.molly, "he": th.text}[u.kind]
        if u.thrower:
            tx, ty = P(u.thrower[0], u.thrower[1])
            mx, my = (tx + lx) / 2, (ty + ly) / 2
            nx, ny = -(ly - ty), (lx - tx)   # yay için dik yönde bükülme
            ln = math.hypot(nx, ny) or 1
            cx, cy = mx + nx / ln * 26, my + ny / ln * 26
            out.append(f'<path d="M{tx:.1f},{ty:.1f} Q{cx:.1f},{cy:.1f} {lx:.1f},{ly:.1f}" fill="none" '
                       f'stroke="{color}" stroke-width="1.4" stroke-dasharray="1 4" stroke-linecap="round"/>')
        if u.kind == "smoke":
            r = SMOKE_RADIUS * s
            out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="{r:.1f}" fill="{th.smoke}" fill-opacity="0.28" '
                       f'stroke="{th.smoke}" stroke-width="1.5"/>')
        elif u.kind == "flash":
            spikes = []
            for k in range(16):
                ang = math.pi * k / 8
                rad = 14 if k % 2 == 0 else 5
                spikes.append(f"{lx + rad * math.cos(ang):.1f},{ly + rad * math.sin(ang):.1f}")
            out.append(f'<polygon points="{" ".join(spikes)}" fill="{th.flash}"/>')
        elif u.kind == "molly":
            out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="{90 * s:.1f}" fill="{th.molly}" fill-opacity="0.30" '
                       f'stroke="{th.molly}" stroke-width="1.5" stroke-dasharray="4 2"/>')
        else:
            out.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="7" fill="none" stroke="{color}" stroke-width="2"/>')
        bx, by = lx + u.label_dx, ly + u.label_dy
        out.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{7.2 * len(u.label) + 10:.1f}" height="17" '
                   f'fill="{th.bg}" fill-opacity="0.9" stroke="{color}" stroke-width="1"/>')
        out.append(f'<text x="{bx + 5:.1f}" y="{by + 12.5:.1f}" fill="{color}" font-family="{th.mono_font}" '
                   f'font-size="11" font-weight="700">{escape(u.label)}</text>')

    # Oyuncu rotaları: kesik çizgi + ok ucu, navmesh'ten gerçek yürüme yolu
    for pl in board.players:
        route = _route(board.map_name, [pl.start, *pl.waypoints])
        px = _offset([P(x, y) for x, y in route], pl.lane)
        color, marker = (th.t, "arrT") if pl.color == T else (th.support, "arrS")
        out.append(f'<path d="{_smooth_path(px)}" fill="none" stroke="{color}" stroke-width="{th.route_width}" '
                   f'stroke-dasharray="9 6" stroke-linecap="round" marker-end="url(#{marker})"/>')

    # Oyuncu tokenları: hedef noktada (rotanın sonu), başlangıçta küçük nokta
    for pl in board.players:
        sx, sy = P(pl.start[0], pl.start[1])
        color, text_color = (th.t, th.t_ink) if pl.color == T else (th.support, th.support_ink)
        out.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="4" fill="{color}" fill-opacity="0.6"/>')
        ex, ey = P(pl.waypoints[-1][0], pl.waypoints[-1][1])
        out.append(f'<circle cx="{ex + 1.5:.1f}" cy="{ey + 2:.1f}" r="13" fill="#000" fill-opacity="0.55"/>')
        out.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="13" fill="{color}" stroke="{th.text}" stroke-width="2"/>')
        out.append(f'<text x="{ex:.1f}" y="{ey + 4.5:.1f}" fill="{text_color}" font-family="{th.mono_font}" '
                   f'font-size="13" font-weight="700" text-anchor="middle">{escape(pl.label)}</text>')
        out.append(f'<text x="{ex + 18:.1f}" y="{ey + 4.5:.1f}" fill="{th.text}" font-family="{th.label_font}" '
                   f'font-size="12" font-weight="700">{escape(pl.name)}</text>')

    out.append("</svg>")
    return "\n".join(out)
