"""Round simülasyonu (A4 prototipi): 5'e 5, rotalar, sıralı düellolar, kazanan takım.

Amaç: "ekip kur, taktik çiz (ör. A rush), düellolar sırayla oynansın, kazanan takım görünsün".
Bu modül yalnız MODEL; arayüz yok. Düello olasılığı yeni bir şey öğrenmez: onaylı modelin
(analysis/rating.py) parçalarını aynen kullanır, simülasyon sadece "kim kimle ne zaman
karşılaşır" ve "sonuçlar nasıl birikir" kısmını ekler.

Mekanik (her adım dt = 0,25 sn):
1. Konum: her oyuncunun zamanlı rotası (`Track`) doğrusal ara değerlenir; rota bitince oyuncu
   olduğu yerde TUTAR (hold). Rota arenada `walk()` ile (ara noktalar + hız) kurulur.
2. Görüş: karşı taraftan her (T, CT) çifti için `geometry.can_see` (awpy ışın izleme). Görüş
   rotalardan belirlendiği için (ölüm hareketi değiştirmez) bütün görüşler Monte Carlo'dan ÖNCE
   bir kez hesaplanır; 2000 tekrar aynı görüş tablosunu paylaşır (süre için kritik).
   Mesafe `MAX_RANGE`'i (verideki en uzak kill: 3.018 birim) aşan çift görmüyor sayılır.
3. Düello: görüş olan ve ikisi de hayatta olan her çift düello yapar. Bir adımda birden çok
   görüşen çift varsa SIRA kuralı: çiftler her tekrarda rastgele (düzgün) sırayla, birbiri
   ardına çözülür; ölen sonraki çiftlere girmez, kazananın canı sonraki çifte taşınır.
   (Eşzamanlı çok kişilik çatışmanın yaklaşığıdır: ilk sıradaki çift "önce vurur".)
4. Olasılık (onaylı model, rating.matchup ile aynı formül):
       logit P(T yener) = taban(0) + bound( β·ΔELO/100 + silah(ΔT−CT, mesafe) + 0,717·Δcan/50 )
   bound = 1,386·tanh(x/1,386) (%20-%80). Silah = rating.weapon_x(simple=True). Can,
   düelloya girerken (o rakipten alınan hasar hariç) can farkı.
5. Hasar: kazanan kalan canla devam eder: kaybedenin ona verdiği hasar, veride gözlenen
   dağılımdan (`WINNER_DAMAGE`, kırpılmış hasar) çekilir; kazanan en az 1 canla kalır.
6. Bomba: `plant=True` oyuncu rotasının sonuna varınca PLANT_SECONDS (3,2 sn) bekler; hayattaysa
   bomba kurulur. Kurulunca TEKRAR bitirilir: sonuç veriden öğrenilmiş plant sonrası tablodan
   (hayatta T − CT farkına göre T kazanma). Plant sonrası düello/retake simüle EDİLMEZ.
7. Round sonu: CT'ler tükenirse T kazanır; plant yokken T'ler tükenirse CT. Ufka (horizon)
   kadar kimse kazanmadıysa, plant yoksa: ufuk tablosu (hayatta fark → T kazanma, veriden).

Sınırlar (dürüstlük): utility (smoke, flash, molotof), trade, momentum, yükseklik avantajı,
peek / tutma ayrımı, ekonomi (sadece silah sınıfı) ve plant sonrası dövüş modelde YOK; ya
doğrulanmadı ya da simüle edilmedi. Oyuncu hareketi savaştan etkilenmez (ölmeyen oyuncu rotasına
devam eder, savaşırken durmaz). Fiziksel sabitler (hız, plant süresi) oyun bilgisidir, tahmin değil.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from squadmind.analysis import rating as rt
from squadmind.analysis.maps import BOARD_PLACES

# --- Fiziksel sabitler (oyun bilgisi) ------------------------------------------------------------

RUN_SPEED = 250.0          # bıçak / koşu hızı (birim / sn); tüfekle ~215, AWP ~200 (kısmen yavaş)
PLANT_SECONDS = 3.2        # bomba kurma süresi (CS2)
DT = 0.25                  # zaman adımı (sn)
MAX_RANGE = 3000.0         # düello menzili: veride en uzak kill 3.018 birim, %99'u 2.717

# --- Veriden öğrenilmiş sabitler (18 Dust2 maçı, 2.853 düello, 398 round; bkz. scripts/eval_simulate.py) ---
# Kazananın düelloda rakipten yediği hasarın ampirik dağılımı (HP, 101 nicelik, kırpılmış hasar).
WINNER_DAMAGE: tuple[float, ...] = (
    0.0,
) * 64 + (
    2.0, 7.0, 11.0, 13.0, 15.0, 17.0, 20.0, 22.0, 23.0, 25.0, 26.0, 26.0, 27.0, 27.0, 29.0, 31.3, 33.0,
    34.9, 37.0, 40.0, 43.0, 45.0, 48.0, 51.0, 53.0, 54.0, 59.0, 61.0, 65.4, 70.0, 73.0, 77.0, 81.0, 85.0,
    88.0, 92.0, 99.0)   # %63'ünde kazanan hiç hasar yemedi; ortalama 15,9
# Son durum tabloları: logit P(T kazanır) = a + b · (hayatta T − hayatta CT)
END_UNPLANTED: tuple[float, float] = (0.069, 1.167)  # ufuk 40 sn, bomba yok (ufuk değişirse yeniden öğrenilmeli)
END_PLANTED: tuple[float, float] = (0.509, 1.346)    # bomba kurulduğu anda
# Silah etkisi katsayıları (rating.WEAPON_FEATURES_SIMPLE sırası: tabanca, awp_uzak, awp_yakın)
WEAPON_BETA: tuple[float, float, float] = (-0.770, 0.834, 0.071)
WEAPON_SE: tuple[float, float, float] = (0.099, 0.171, 0.117)
BETA_ELO = 0.047           # logit / 100 ELO (rating.py: 0,047 ± 0,007; bu sınamada 0,0469 ± 0,0082)
BETA_ELO_SE = 0.008

# Sonuç kodları (SimResult.outcomes anahtarları)
_ENDS = ("ct_elendi", "t_elendi", "plant", "ufuk")


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def duel_p(d_elo, wx, hp_t, hp_c, beta, weapon, hp_coef, base=0.0):
    """P(T yener): rating.matchup ile aynı formül (vektörel). d_elo = ELO_T − ELO_CT, wx = weapon_x(simple)
    (T − CT), hp_t / hp_c düelloya girerken can; logit = taban + bound(β·Δ/100 + silah + hp_coef·Δcan/50)."""
    hd = (np.clip(hp_t, 1, 100) - np.clip(hp_c, 1, 100)) / rt.HP_UNIT
    delta = beta * np.asarray(d_elo) / rt.ELO_UNIT + (np.asarray(weapon) * np.asarray(wx)).sum(axis=-1) + hp_coef * hd
    return _sigmoid(base + rt.MAX_LOGIT * np.tanh(delta / rt.MAX_LOGIT))


# --- Rota ----------------------------------------------------------------------------------------


class Track:
    """Zamanlı rota: (t, x, y, z) noktaları; arası doğrusal, öncesi ilk, sonrası son nokta (hold).

    z bilinmiyorsa (arena: sadece x, y) NaN tutulur; görüşte navmesh katlarından denenir.
    """

    def __init__(self, t: Sequence[float], xyz: Sequence[Sequence[float | None]]) -> None:
        if len(t) == 0 or len(t) != len(xyz):
            raise ValueError("rota boş ya da t / konum sayıları farklı")
        order = np.argsort(np.asarray(t, dtype=float), kind="stable")
        self.t = np.asarray(t, dtype=float)[order]
        pts = np.full((len(t), 3), np.nan)
        for i, p in enumerate(xyz):
            pts[i, 0], pts[i, 1] = p[0], p[1]
            if len(p) > 2 and p[2] is not None:
                pts[i, 2] = p[2]
        self.xyz = pts[order]

    @property
    def end(self) -> float:
        """Rotanın bittiği an (sonrası tutma)."""
        return float(self.t[-1])

    def at(self, times: np.ndarray) -> np.ndarray:
        """Verilen anlardaki konumlar (len(times), 3); z hepsi NaN ise NaN kalır."""
        out = np.empty((len(times), 3))
        for c in range(3):
            col = self.xyz[:, c]
            if np.isnan(col).all():
                out[:, c] = np.nan
            else:
                ok = ~np.isnan(col)
                out[:, c] = np.interp(times, self.t[ok], col[ok])
        return out


def walk(points: Sequence[Sequence[float]], speed: float = RUN_SPEED, start_delay: float = 0.0) -> Track:
    """Ara noktalardan sabit hızla rota: `points[0]`'da `start_delay` sn bekler, sonra yürür.

    Mesafe yatay (x, y). Tek nokta = tutma noktası. CT rotasyonu: [tutma, hedef] ve
    `start_delay` = rotasyonun başladığı an.
    """
    if not points:
        raise ValueError("en az bir nokta gerekir")
    t, xyz = [0.0], [tuple(points[0])]
    if start_delay > 0:
        t.append(start_delay)
        xyz.append(tuple(points[0]))
    for a, b in zip(points, points[1:], strict=False):
        t.append(t[-1] + math.hypot(b[0] - a[0], b[1] - a[1]) / speed)
        xyz.append(tuple(b))
    return Track(t, xyz)


@dataclass(frozen=True)
class Unit:
    """Bir oyuncu: ad, ELO (kullanılan güç; arena: canlı FACEIT ELO), silah sınıfı, rota."""

    name: str
    elo: float
    track: Track
    cls: str = "rifle"          # rifle / pistol / smg / awp / scout / heavy
    plant: bool = False         # rotasının sonunda bomba kurar (sadece T)


@dataclass(frozen=True)
class Scenario:
    map_name: str
    t: tuple[Unit, ...]
    ct: tuple[Unit, ...]
    horizon: float = 45.0       # simüle edilen süre (sn); sonrası ufuk tablosu
    dt: float = DT


# --- Parametreler --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DuelParams:
    """Onaylı düello modelinin sayıları + veriden öğrenilen son durum tabloları.

    `beta` / `weapon` / `hp_coef` rating.py ile aynı anlamda (logit). `*_se` parametre
    belirsizliği için (grup başına bir çekiliş); `damage` kazananın hasar örneği.
    """

    beta: float = BETA_ELO
    beta_se: float = BETA_ELO_SE
    weapon: tuple[float, float, float] = WEAPON_BETA
    weapon_se: tuple[float, float, float] = WEAPON_SE
    hp_coef: float = rt.HP_COEF
    hp_se: float = rt.HP_SE
    base: float = 0.0
    max_range: float = MAX_RANGE
    damage: tuple[float, ...] = WINNER_DAMAGE
    end_unplanted: tuple[float, float] = END_UNPLANTED
    end_planted: tuple[float, float] = END_PLANTED


def fit_end_table(diff: Sequence[float], y: Sequence[float], ridge: float = 2.0) -> tuple[float, float]:
    """logit P(T) = a + b·diff (hayatta T − CT); hafif ridge (b'yi küçültür), a serbest."""
    X = np.column_stack([np.ones(len(diff)), np.asarray(diff, dtype=float)])
    if len(y) == 0:
        return 0.0, 0.0
    pen = np.array([1e-6, ridge])
    beta, _ = rt._fit_logistic(X, np.zeros(len(y)), np.asarray(y, dtype=float), pen)
    return float(beta[0]), float(beta[1])


# --- Görüş ---------------------------------------------------------------------------------------


def geometry_los(map_name: str) -> Callable[[np.ndarray, np.ndarray], bool]:
    """Awpy ışın izleme ile görüş (a, b = x, y, z; z NaN ise navmesh katlarından herhangi bir çift).

    Sonuçlar 4 birimlik yuvarlamayla önbelleğe alınır (rotalar sabit, tekrar eden çiftler çok).
    """
    from squadmind.analysis import geometry as geo

    cache: dict[tuple, bool] = {}
    floors: dict[tuple, list[float]] = {}

    def zs(p: np.ndarray) -> list[float]:
        if not math.isnan(p[2]):
            return [float(p[2])]
        key = (round(p[0] / 4), round(p[1] / 4))
        if key not in floors:
            floors[key] = geo.floor_zs(map_name, float(p[0]), float(p[1])) or [0.0]
        return floors[key]

    def los(a: np.ndarray, b: np.ndarray) -> bool:
        key = (round(a[0] / 4), round(a[1] / 4), None if math.isnan(a[2]) else round(a[2] / 4),
               round(b[0] / 4), round(b[1] / 4), None if math.isnan(b[2]) else round(b[2] / 4))
        hit = cache.get(key)
        if hit is None:
            hit = any(geo.can_see(map_name, (a[0], a[1], za), (b[0], b[1], zb))
                      for za in zs(a) for zb in zs(b))
            cache[key] = hit
        return hit

    return los


# --- Hazırlık (görüş tablosu; Monte Carlo'dan bağımsız) ---------------------------------------------


@dataclass
class Prepared:
    """Senaryonun deterministik kısmı: her adımda görüşen (T, CT) çiftleri ve sabit terimleri."""

    scn: Scenario
    n_steps: int
    step_ptr: np.ndarray       # (n_steps + 1,) çiftlerin adıma göre dilimi
    pair_i: np.ndarray         # T oyuncu sırası
    pair_j: np.ndarray         # CT oyuncu sırası
    pair_dist: np.ndarray
    pair_mid: np.ndarray       # (M, 2) çiftin orta noktası
    pair_wx: np.ndarray        # (M, 3) rating.weapon_x(simple) (T − CT)
    pair_delo: np.ndarray      # (M,) ELO_T − ELO_CT
    plant_ready: np.ndarray    # (nT,) bomba kurulum bitişi (sn); planter değilse inf
    elapsed: float = 0.0


def prepare(scn: Scenario, los: Callable[[np.ndarray, np.ndarray], bool] | None = None,
            max_range: float = MAX_RANGE) -> Prepared:
    """Adım adım konumlar + görüş (ışın izleme). `los` verilmezse haritanın geometrisi."""
    t0 = time.perf_counter()
    los = los or geometry_los(scn.map_name)
    n_steps = int(round(scn.horizon / scn.dt)) + 1
    times = np.arange(n_steps) * scn.dt
    pt = np.stack([u.track.at(times) for u in scn.t], axis=1)    # (K, nT, 3)
    pc = np.stack([u.track.at(times) for u in scn.ct], axis=1)   # (K, nC, 3)
    ptr, pi, pj, dist, mid, wx, de = [0], [], [], [], [], [], []
    for k in range(n_steps):
        for i, ut in enumerate(scn.t):
            for j, uc in enumerate(scn.ct):
                a, b = pt[k, i], pc[k, j]
                d = math.hypot(a[0] - b[0], a[1] - b[1])
                if d > max_range or not los(a, b):
                    continue
                pi.append(i); pj.append(j); dist.append(d)
                mid.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
                wx.append(rt.weapon_x(ut.cls, uc.cls, d, simple=True))
                de.append(ut.elo - uc.elo)
        ptr.append(len(pi))
    ready = np.array([u.track.end + PLANT_SECONDS if u.plant else np.inf for u in scn.t])
    return Prepared(scn, n_steps, np.array(ptr), np.array(pi, dtype=int), np.array(pj, dtype=int),
                    np.array(dist), np.array(mid).reshape(-1, 2), np.array(wx).reshape(-1, 3),
                    np.array(de), ready, time.perf_counter() - t0)


# --- Monte Carlo ---------------------------------------------------------------------------------


@dataclass
class SimResult:
    """Simülasyon sonucu. `p_t` = T'nin round'u kazanma olasılığı (tekrarlar üzerinde ortalama)."""

    n: int
    p_t: float
    lo: float                                  # %90 aralık (Monte Carlo + parametre belirsizliği)
    hi: float
    outcomes: dict[str, float]                 # round nasıl bitti (oran)
    t_names: tuple[str, ...]
    ct_names: tuple[str, ...]
    t_death: np.ndarray                        # (nT,) ölme olasılığı
    ct_death: np.ndarray
    t_kills: np.ndarray                        # (nT,) ortalama kill
    ct_kills: np.ndarray
    t_kill_dist: np.ndarray                    # (nT, nC + 1) kill sayısı dağılımı
    ct_kill_dist: np.ndarray
    first_duel_rate: float                     # en az bir düello olan tekrarların oranı
    first_duel_time: float                     # ilk düelloların ortalama anı (sn)
    first_duel_pairs: dict[tuple[str, str], float]
    first_duel_cells: dict[tuple[int, int], float]   # 128 birimlik hücre → oran (tüm tekrarlara)
    first_duel_places: dict[str, float]        # en yakın harita etiketine göre
    plant_rate: float
    elapsed: float                             # MC süresi (sn); hazırlık ayrı: Prepared.elapsed


def nearest_place(map_name: str, x: float, y: float) -> str:
    """Haritadaki en yakın etiket (BOARD_PLACES) adı; etiket yoksa 'bilinmiyor'."""
    places = BOARD_PLACES.get(map_name, {})
    if not places:
        return "bilinmiyor"
    return min(places, key=lambda k: math.hypot(places[k][0] - x, places[k][1] - y))


def run(prep: Prepared, params: DuelParams | None = None, n: int = 2000, seed: int = 0,
        groups: int = 20, uncertainty: bool = True) -> SimResult:
    """`n` tekrar; aynı `seed` aynı sonucu verir. `uncertainty`: parametre belirsizliğini aralığa kat."""
    t0 = time.perf_counter()
    params = params or DuelParams()
    rng = np.random.default_rng(seed)
    scn = prep.scn
    nT, nC = len(scn.t), len(scn.ct)
    ar = np.arange(n)
    alive_t, alive_c = np.ones((n, nT), bool), np.ones((n, nC), bool)
    hp_t, hp_c = np.full((n, nT), 100.0), np.full((n, nC), 100.0)
    kills_t, kills_c = np.zeros((n, nT), int), np.zeros((n, nC), int)
    done = np.zeros(n, bool)
    q = np.zeros(n)
    ended = np.zeros(n, int)
    first_k = np.full(n, -1)
    first_pair = np.zeros((n, 2), int)
    first_mid = np.zeros((n, 2))
    damage = np.asarray(params.damage, dtype=float)

    # Parametreler: grup başına bir çekiliş (belirsizlik); kapalıysa nokta değerleri
    g_of = (ar * groups) // n
    if uncertainty:
        beta_g = params.beta + params.beta_se * rng.standard_normal(groups)
        hp_g = params.hp_coef + params.hp_se * rng.standard_normal(groups)
        w_g = np.asarray(params.weapon) + np.asarray(params.weapon_se) * rng.standard_normal((groups, 3))
    else:
        beta_g = np.full(groups, params.beta)
        hp_g = np.full(groups, params.hp_coef)
        w_g = np.tile(np.asarray(params.weapon), (groups, 1))
    beta_r, hp_r, w_r = beta_g[g_of], hp_g[g_of], w_g[g_of]
    end_u, end_p = params.end_unplanted, params.end_planted

    for k in range(prep.n_steps):
        a, b = prep.step_ptr[k], prep.step_ptr[k + 1]
        m = b - a
        if m:
            order = rng.random((n, m)).argsort(axis=1)    # her tekrarda rastgele sıra
            for s in range(m):
                idx = a + order[:, s]
                pi, pj = prep.pair_i[idx], prep.pair_j[idx]
                act = ~done & alive_t[ar, pi] & alive_c[ar, pj]
                rows = np.nonzero(act)[0]
                if rows.size == 0:
                    continue
                ri, rj, ix = pi[rows], pj[rows], idx[rows]
                p = duel_p(prep.pair_delo[ix], prep.pair_wx[ix], hp_t[rows, ri], hp_c[rows, rj],
                           beta_r[rows], w_r[rows], hp_r[rows], params.base)
                t_wins = rng.random(rows.size) < p
                # İlk düello kaydı
                fresh = first_k[rows] < 0
                if fresh.any():
                    fr = rows[fresh]
                    first_k[fr] = k
                    first_pair[fr, 0], first_pair[fr, 1] = ri[fresh], rj[fresh]
                    first_mid[fr] = prep.pair_mid[ix[fresh]]
                dmg = damage[rng.integers(0, len(damage), rows.size)] if damage.size else np.zeros(rows.size)
                tw, cw = rows[t_wins], rows[~t_wins]
                if tw.size:       # T kazandı: CT ölür, T'nin canı düşer (en az 1)
                    alive_c[tw, rj[t_wins]] = False
                    kills_t[tw, ri[t_wins]] += 1
                    hp_t[tw, ri[t_wins]] = np.maximum(1.0, hp_t[tw, ri[t_wins]] - dmg[t_wins])
                if cw.size:
                    alive_t[cw, ri[~t_wins]] = False
                    kills_c[cw, rj[~t_wins]] += 1
                    hp_c[cw, rj[~t_wins]] = np.maximum(1.0, hp_c[cw, rj[~t_wins]] - dmg[~t_wins])
        # Round sonu kontrolleri (bu adımın düelloları bittikten sonra)
        live_t, live_c = alive_t.sum(axis=1), alive_c.sum(axis=1)
        e1 = ~done & (live_c == 0)
        q[e1], ended[e1], done[e1] = 1.0, 1, True
        e2 = ~done & (live_t == 0)
        q[e2], ended[e2], done[e2] = 0.0, 2, True
        now = k * scn.dt
        for i in np.nonzero(prep.plant_ready <= now + 1e-9)[0]:
            pl_ = ~done & alive_t[:, i]
            if pl_.any():
                q[pl_] = _sigmoid(end_p[0] + end_p[1] * (live_t[pl_] - live_c[pl_]))
                ended[pl_], done[pl_] = 3, True
        if done.all():
            break
    rest = ~done
    if rest.any():
        diff = alive_t[rest].sum(axis=1) - alive_c[rest].sum(axis=1)
        q[rest] = _sigmoid(end_u[0] + end_u[1] * diff)
        ended[rest] = 4

    p_t = float(q.mean())
    se_mc = float(q.std(ddof=1) / math.sqrt(n)) if n > 1 else 0.0
    gm = np.array([q[g_of == g].mean() for g in range(groups)])
    # Gruplar arası varyans = parametre varyansı + grup MC gürültüsü (q.var / (n/groups)); gürültü çıkarılır
    var_param = max(0.0, float(gm.var(ddof=1)) - float(q.var()) * groups / n) if uncertainty else 0.0
    sd = math.sqrt(se_mc ** 2 + var_param)
    lo, hi = max(0.0, p_t - rt.Z90 * sd), min(1.0, p_t + rt.Z90 * sd)

    # Betimleyici çıktılar
    t_names, ct_names = tuple(u.name for u in scn.t), tuple(u.name for u in scn.ct)
    had = first_k >= 0
    pairs: Counter = Counter()
    cells: Counter = Counter()
    places: Counter = Counter()
    for (i, j), c in Counter(map(tuple, first_pair[had].tolist())).items():
        pairs[(t_names[i], ct_names[j])] = c / n
    for (cx, cy), c in Counter(map(tuple, np.floor(first_mid[had] / 128).astype(int).tolist())).items():
        cells[(int(cx) * 128 + 64, int(cy) * 128 + 64)] = c / n
        places[nearest_place(scn.map_name, cx * 128 + 64, cy * 128 + 64)] += c / n
    return SimResult(
        n=n, p_t=p_t, lo=lo, hi=hi,
        outcomes={name: float((ended == c + 1).mean()) for c, name in enumerate(_ENDS)},
        t_names=t_names, ct_names=ct_names,
        t_death=1 - alive_t.mean(axis=0), ct_death=1 - alive_c.mean(axis=0),
        t_kills=kills_t.mean(axis=0), ct_kills=kills_c.mean(axis=0),
        t_kill_dist=np.stack([np.bincount(kills_t[:, i], minlength=nC + 1)[:nC + 1] / n for i in range(nT)]),
        ct_kill_dist=np.stack([np.bincount(kills_c[:, j], minlength=nT + 1)[:nT + 1] / n for j in range(nC)]),
        first_duel_rate=float(had.mean()),
        first_duel_time=float((first_k[had] * scn.dt).mean()) if had.any() else float("nan"),
        first_duel_pairs=dict(sorted(pairs.items(), key=lambda kv: -kv[1])),
        first_duel_cells=dict(sorted(cells.items(), key=lambda kv: -kv[1])),
        first_duel_places=dict(sorted(places.items(), key=lambda kv: -kv[1])),
        plant_rate=float((ended == 3).mean()),
        elapsed=time.perf_counter() - t0,
    )


def simulate(scn: Scenario, params: DuelParams | None = None, n: int = 2000, seed: int = 0,
             los: Callable[[np.ndarray, np.ndarray], bool] | None = None) -> SimResult:
    """Tek çağrıda hazırlık + Monte Carlo (arena: ikisinin toplam süresi < 2 sn hedefi)."""
    return run(prepare(scn, los, (params or DuelParams()).max_range), params, n, seed)
