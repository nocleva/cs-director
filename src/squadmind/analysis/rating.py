"""Oyuncu gücü (FACEIT ELO + stat puanı) ve düello karşılaşma olasılığı, belirsizliğiyle.

Taktik oluşturucunun "bu hatta A, B'yi yener mi" sorusunun sayısal temeli. Kullanıcının isteği:
oyuncunun istatistiklerinden bir puan, FACEIT puanıyla birleşsin; çok düşüğü ezmesin, çok
yükseği uçurmasın, kararlı olsun, güvenilirliği açık olsun.

Yöntem (formüller; sabitlerin gerekçesi aşağıda, sayılar 18 Dust2 demosunun sınamasından):

1. Stat puanı (hiyerarşik / ampirik Bayes küçültme). Oyuncunun son ≤100 FACEIT maçından
   kill payı  pay = ΣK / (ΣK + ΣD)  (≈ kazandığı "düello" oranı). Seviyesi ön bilgidir:
       beklenen μ(ELO) = a + b·ELO             (popülasyon regresyonu, sonuç verisi kullanmaz)
       ŝ = μ + w·(pay − μ),  w = τ² / (τ² + σ²_maç / n)
   τ² = oyuncular arası gerçek fark, σ²_maç = aynı oyuncunun maçtan maça oynaması (veriden;
   τ ≈ 0,027, σ_maç ≈ 0,10 → 10 maçta w ≈ 0,39, 99 maçta w ≈ 0,86). Az maçta seviye konuşur.
2. Birleşik ELO: stat sapması, popülasyon eğimiyle ELO'ya çevrilir, κ ile kısılır, sınırlanır:
       birleşik = ELO + 300·tanh( κ·(ŝ − μ)/b / 300 ),   κ = 0,25
   (b ≈ 4,4e-5 → +0,01 pay ≈ +229 ELO ham; κ ile ≈ +57). Ek, ±300 ELO'yu (≈ 1,5 seviye)
   asla aşmaz: kötü bir maç serisi oyuncuyu ezemez, iyi seri uçuramaz.
3. Karşılaşma: logit P(A yener) = taban + 1,386·tanh( β·Δbirleşik/100 / 1,386 )
   β düello sonuçlarından (lojistik regresyon, hafif ridge) öğrenilir: 2.853 Dust2 düellosunda
   0,047 ± 0,007 / 100 ELO (katlarda 0,042-0,063; kararlı).
   tanh sınırı: hiçbir eşleşme %20'nin altına / %80'in üstüne çıkmaz (logit 0,8 = 1,386).
   Taban varsayılanı %50: hat × taraf tabanı sınamada yazı-turadan KÖTÜ çıktı.
4. Belirsizlik: Var(Δ) = gᵀ Cov(β) g + β²·(Var_A + Var_B)  (ELO aralığı, küçültülmüş stat
   varyansı) → %90 aralık. Güven etiketi: "az veri" (ELO bilinmiyor ya da <20 FACEIT maçı),
   "belirgin" (aralık %50'yi dışlıyor: model kimin favori olduğundan emin), "belirsiz".

Sınamada (bkz. tests/ ve rapor) katkısı görülmeyen ve bu yüzden varsayılanda KAPALI olanlar:
ADR, demo düello artığı, hat × taraf artığı, mesafe dilimi tabanı. Arayüzde açılabilirler.
Sınırlar: düello = kill'le biten temas; peek / tutma modelde yok (ölçülemedi); silah etkisi
opsiyonel ve varsayılan kapalı (bölüm 3b); aralık
güç tahmininin belirsizliğidir, tek düellonun şansı değil (olasılıklar çoğunlukla %40-60).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

# FACEIT CS2 seviye → ELO aralığı (FACEIT Yardım Merkezi "Skill levels" tablosu: 1 = 100-500,
# 2 = 501-750, 3 = 751-900, 4 = 901-1050, 5 = 1051-1200, 6 = 1201-1350, 7 = 1351-1530,
# 8 = 1531-1750, 9 = 1751-2000, 10 = 2001+). Seviye 10 üstten açık: 2001-2400 sayıyoruz (bu
# lobilerdeki seviye 10'ların tipik bandı). Seviye 0 / boş = bilinmiyor.
LEVEL_ELO_RANGE: dict[int, tuple[float, float]] = {
    1: (100, 500), 2: (501, 750), 3: (751, 900), 4: (901, 1050), 5: (1051, 1200),
    6: (1201, 1350), 7: (1351, 1530), 8: (1531, 1750), 9: (1751, 2000), 10: (2001, 2400),
}
ELO_UNIT = 100.0          # ELO özellikleri 100 puan birimiyle (katsayı okunaklı olsun)
ADR_UNIT = 10.0           # ADR 10 hasar birimiyle
# Stat → ELO kısma katsayısı. Zaman bölmeli sınamada κ ∈ {0; 0,1; 0,25; 0,5; 1} denendi;
# 0,1-0,5 arası düz (log-kayıp farkı < 0,0003), 0 ve 1 daha kötü → ortası 0,25.
KAPPA = 0.25
# Stat ekinin üst sınırı (ELO). ~1,5 seviye: tek bir istatistik serisi oyuncuyu iki seviye
# oynatmasın. κ = 0,25 ile verideki en uç ek ≈ ±150, sınır pratikte sadece uç girdide çalışır.
MAX_STAT_ADJ = 300.0
# Tek düello sınırı (logit): taban %50 iken olasılık [%20, %80]. Sınamada en uç tahmin %31-%69,
# yani sınır gerçek veride neredeyse hiç çalışmaz; sadece veri dışı uç girdiyi (ör. ELO 4000) tutar.
MAX_LOGIT = math.log(0.8 / 0.2)
# Taban (hat × taraf) küçültme ağırlığı (düello). Sadece base="line" seçilince kullanılır.
BASE_PRIOR = 50.0
# Demo / hat artıklarının ön bilgi sapması (log-odds); sadece açılırsa. FACEIT kademesi ~100
# maçla zaten güçlü: demo artığının ±0,15'ten (≈ ±4 puan düello oranı) büyük olması beklenmez.
TAU_DEMO = 0.15
TAU_LINE = 0.10
RIDGE = 1.0               # β ~ N(0, 1/RIDGE): özellikler ~birim ölçekli, hafif düzenleme
Z90 = 1.645               # %90 aralık
# "az veri" eşiği: 20 maçta w ≈ 0,56 → oyuncunun verisi seviyesi kadar ağır basmaya başlar
MIN_MATCHES = 20
# Özellik sırası: x[0] ELO, x[1] logit(pay), x[2] ADR, x[3] birleşik ELO
FEATURES = ("elo", "share", "adr", "combined")
DEFAULT_USE = (False, False, False, True)


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def bound(delta: float, m: float = MAX_LOGIT) -> float:
    """Yumuşak sınır: küçük farkı neredeyse aynen bırakır, büyüğü ±m'ye yaklaştırır (en fazla m)."""
    return m * math.tanh(delta / m)


def level_elo(level: int | None) -> tuple[float | None, float]:
    """Seviyeden ELO tahmini ve varyansı (aralıkta düzgün dağılım: genişlik² / 12)."""
    if not level or level not in LEVEL_ELO_RANGE:
        return None, 0.0
    lo, hi = LEVEL_ELO_RANGE[level]
    return (lo + hi) / 2, (hi - lo) ** 2 / 12


# --- 1. FACEIT geçmişi ve popülasyon ön bilgisi ----------------------------------------------


@dataclass(frozen=True)
class History:
    """FACEIT maç geçmişinin özeti (maç başına kill payı ve ADR listeleri)."""

    shares: tuple[float, ...]   # maç başına K / (K + D)
    adrs: tuple[float, ...]     # maç başına ADR (olan maçlar)
    kills: int = 0
    deaths: int = 0

    @property
    def n(self) -> int:
        return len(self.shares)

    @property
    def share(self) -> float | None:
        """Toplam kill payı (maç ortalaması değil: uzun maçlar daha çok bilgi taşır)."""
        return self.kills / (self.kills + self.deaths) if self.kills + self.deaths else None

    @property
    def adr(self) -> float | None:
        return sum(self.adrs) / len(self.adrs) if self.adrs else None


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize_history(match_stats: Iterable[Mapping[str, Any]]) -> History:
    """FACEIT `games/cs2/stats` maç satırlarından özet (eksik / bozuk satırlar atlanır)."""
    shares, adrs, kills, deaths = [], [], 0, 0
    for m in match_stats:
        k, d = _num(m.get("Kills")), _num(m.get("Deaths"))
        if k is None or d is None or k < 0 or d < 0 or k + d == 0:
            continue
        shares.append(k / (k + d))
        kills += int(k)
        deaths += int(d)
        adr = _num(m.get("ADR"))
        if adr is not None and adr > 0:
            adrs.append(adr)
    return History(tuple(shares), tuple(adrs), kills, deaths)


@dataclass(frozen=True)
class StatPrior:
    """Bir istatistiğin popülasyon modeli: beklenen = a + b·ELO, oyuncular arası τ², maç içi σ²."""

    a: float
    b: float
    tau2: float
    s2: float
    mean: float
    var: float   # ELO bilinmeyende ön bilgi varyansı (toplam popülasyon varyansı)

    def shrink(self, raw: float | None, n: int, elo: float | None) -> tuple[float, float, float]:
        """(küçültülmüş değer, son varyans, veri ağırlığı w). n = 0 ise değer = ön bilgi."""
        mu = self.a + self.b * elo if elo is not None else self.mean
        prior_var = self.tau2 if elo is not None else self.var
        if raw is None or n <= 0:
            return mu, prior_var, 0.0
        samp = self.s2 / n
        w = prior_var / (prior_var + samp)
        return mu + w * (raw - mu), w * samp, w


@dataclass(frozen=True)
class Population:
    """FACEIT istatistiklerinin seviyeye göre beklentisi (ampirik Bayes ön bilgileri)."""

    share: StatPrior
    adr: StatPrior
    elo_mean: float
    elo_var: float
    n_players: int


def _fit_prior(values: list[tuple[float, int, float | None]], s2: float) -> StatPrior:
    """values: (ham değer, maç sayısı, ELO). Momentler: τ² = artık varyans − ort. örnekleme varyansı."""
    arr = [(v, n, e) for v, n, e in values if n > 0]
    if not arr:
        return StatPrior(0.5, 0.0, 1e-3, s2, 0.5, 1e-3)
    ys = np.array([v for v, _, _ in arr])
    known = [(v, n, e) for v, n, e in arr if e is not None]
    if len(known) >= 5:
        x = np.array([e for _, _, e in known])
        y = np.array([v for v, _, _ in known])
        b, a = np.polyfit(x, y, 1)
        resid = y - (a + b * x)
        samp = float(np.mean([s2 / n for _, n, _ in known]))
        # Taban 1e-5: τ² sıfıra düşerse herkes ortalamaya yapışırdı (w = 0)
        tau2 = max(float(np.var(resid)) - samp, 1e-5)
    else:
        a, b, tau2 = float(np.mean(ys)), 0.0, max(float(np.var(ys)), 1e-5)
    var = max(float(np.var(ys)), tau2)
    return StatPrior(float(a), float(b), float(tau2), float(s2), float(np.mean(ys)), var)


def _within_var(series: Iterable[tuple[float, ...]]) -> float:
    """Oyuncu içi (maçtan maça) havuzlanmış varyans."""
    ss, dof = 0.0, 0
    for xs in series:
        if len(xs) >= 2:
            m = sum(xs) / len(xs)
            ss += sum((x - m) ** 2 for x in xs)
            dof += len(xs) - 1
    return ss / dof if dof else 0.01


def fit_population(players: Iterable[tuple[History, float | None]]) -> Population:
    """Oyuncu geçmişlerinden (History, ELO) ön bilgi parametreleri. Düello sonucu kullanmaz."""
    ps = list(players)
    share = _fit_prior([(h.share, h.n, e) for h, e in ps if h.share is not None],
                       _within_var(h.shares for h, _ in ps))
    adr = _fit_prior([(h.adr, len(h.adrs), e) for h, e in ps if h.adr is not None],
                     _within_var(h.adrs for h, _ in ps))
    elos = [e for _, e in ps if e is not None]
    return Population(share, adr, float(np.mean(elos)) if elos else 1500.0,
                      float(np.var(elos)) if len(elos) > 1 else 300.0 ** 2, len(ps))


# --- 2. Oyuncu özellikleri (stat puanı + birleşik ELO) ----------------------------------------


@dataclass(frozen=True)
class PlayerFeatures:
    """Bir oyuncunun FACEIT kademesi: puanlar, model girdileri ve kaynak sayıları."""

    x: tuple[float, float, float, float]    # (ELO, logit pay, ADR, birleşik ELO), merkezli + birimli
    var: tuple[float, float, float, float]  # her girdinin belirsizliği (aynı birimde)
    elo: float                              # kullanılan ELO (bilinmiyorsa popülasyon ortalaması)
    elo_known: bool
    share: float                            # küçültülmüş kill payı (stat puanı)
    share_raw: float | None
    adr: float                              # küçültülmüş ADR
    weight: float                           # oyuncunun kendi verisinin ağırlığı w (0-1)
    n_matches: int
    stat_adj: float                         # stat puanının ELO karşılığı (κ'lı, sınırlı)
    combined_elo: float                     # birleşik ELO = ELO + stat_adj
    combined_sd: float                      # birleşik ELO'nun belirsizliği (ELO)
    elo_exact: bool = False                 # ELO FACEIT'ten kesin mi (False: seviye aralığı ortası)


def player_features(hist: History, pop: Population, *, elo: float | None = None,
                    level: int | None = None, kappa: float = KAPPA,
                    max_adj: float = MAX_STAT_ADJ) -> PlayerFeatures:
    """FACEIT özetinden puanlar. ELO verilirse o (kesin), yoksa seviyeden (aralık ortası)."""
    if elo is not None:
        e, e_var = float(elo), 0.0
    else:
        e, e_var = level_elo(level)
    known = e is not None
    s, s_var, w = pop.share.shrink(hist.share, hist.n, e)
    a, a_var, _ = pop.adr.shrink(hist.adr, len(hist.adrs), e)
    if known:
        # Seviyeden gelen ELO belirsizliği beklenen istatistiği de oynatır (b·σ_ELO)
        s_var += pop.share.b ** 2 * e_var
        a_var += pop.adr.b ** 2 * e_var
    else:
        e, e_var = pop.elo_mean, pop.elo_var
    s = min(max(s, 0.05), 0.95)
    # Stat puanı → ELO karşılığı: seviyesinin beklediğinden sapma / eğim; κ ile kısılır, sınırlanır.
    # ELO bilinmiyorsa stat ELO'ya eklenmez (sapmanın neye göre olduğu belli değil): w zaten küçük.
    b = pop.share.b
    if known and b > 0 and max_adj > 0:
        raw_adj = kappa * (s - (pop.share.a + b * e)) / b
        adj = bound(raw_adj, max_adj)
        slope = kappa * (1 - math.tanh(raw_adj / max_adj) ** 2) / b   # sınırın türevi
        adj_var = slope ** 2 * s_var
    else:
        adj, adj_var = 0.0, 0.0
    comb = e + adj
    comb_var = e_var + adj_var
    d = s * (1 - s)   # delta yöntemi: logit'in türevi 1 / (p(1−p))
    x = ((e - pop.elo_mean) / ELO_UNIT, _logit(s) - _logit(pop.share.mean),
         (a - pop.adr.mean) / ADR_UNIT, (comb - pop.elo_mean) / ELO_UNIT)
    var = (e_var / ELO_UNIT ** 2, s_var / d ** 2, a_var / ADR_UNIT ** 2, comb_var / ELO_UNIT ** 2)
    return PlayerFeatures(x, var, e, known, s, hist.share, a, w, hist.n, adj, comb, math.sqrt(comb_var),
                          elo is not None)


# --- 3. Model ---------------------------------------------------------------------------------

OTHER = "_"   # hat dışı düellolar


def _line_key(line: str | None) -> str:
    return line if line else OTHER


@dataclass
class RatingModel:
    """Öğrenilmiş model: taban, β (ve kovaryansı), açılırsa demo / hat artıkları."""

    base_t: dict[str, float]                 # hat → T kazanma logit'i ("_" = hat dışı / genel)
    beta: np.ndarray
    cov: np.ndarray
    use: tuple[bool, ...]
    delta: dict[str, tuple[float, float, int]] = field(default_factory=dict)            # oyuncu → (δ, var, n)
    lam: dict[tuple[str, str, str], tuple[float, float, int]] = field(default_factory=dict)  # (oyuncu, hat, taraf)
    base_band: dict[tuple[str, str], float] = field(default_factory=dict)              # (hat, dilim) → logit
    n_duels: int = 0


def fit_base(du: pl.DataFrame, mode: str = "none", prior: float = BASE_PRIOR
             ) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
    """T kazanma tabanı. mode: "none" (%50), "side" (genel T oranı), "line" (hat başına,
    genel orana `prior` düello ağırlığında küçültülmüş; ayrıca hat × mesafe dilimi)."""
    if mode == "none" or du.height == 0:
        return {OTHER: 0.0}, {}
    d = du.with_columns((pl.col("winner_side") == "T").cast(pl.Int64).alias("tw"),
                        pl.col("line").fill_null(OTHER))
    p_all = float(d["tw"].mean())
    base: dict[str, float] = {OTHER: _logit(p_all)}
    band: dict[tuple[str, str], float] = {}
    if mode == "side":
        return base, band
    for r in d.group_by("line").agg(pl.col("tw").sum().alias("w"), pl.len().alias("n")).iter_rows(named=True):
        base[r["line"]] = _logit((r["w"] + prior * p_all) / (r["n"] + prior))
    if "band" in d.columns:
        for r in d.group_by("line", "band").agg(pl.col("tw").sum().alias("w"),
                                                pl.len().alias("n")).iter_rows(named=True):
            p_line = _sigmoid(base[r["line"]])
            band[(r["line"], r["band"])] = _logit((r["w"] + prior * p_line) / (r["n"] + prior))
    return base, band


def _pick(v: tuple[float, ...], use: tuple[bool, ...]) -> np.ndarray:
    return np.array([v[i] for i in range(len(use)) if use[i]], dtype=float)


def _fit_logistic(X: np.ndarray, off: np.ndarray, y: np.ndarray, ridge: float
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Ofsetli ridge lojistik regresyon (Newton-Raphson). Döner: β, kovaryans (ters Hessian)."""
    k = X.shape[1]
    beta = np.zeros(k)
    for _ in range(50):
        p = 1 / (1 + np.exp(-(off + X @ beta)))
        grad = X.T @ (y - p) - ridge * beta
        H = (X * (p * (1 - p))[:, None]).T @ X + ridge * np.eye(k)
        step = np.linalg.solve(H, grad)
        beta += step
        if np.max(np.abs(step)) < 1e-9:
            break
    p = 1 / (1 + np.exp(-(off + X @ beta)))
    H = (X * (p * (1 - p))[:, None]).T @ X + ridge * np.eye(k)
    return beta, np.linalg.inv(H)


def _residuals(keys_a: list, keys_b: list, logit0: np.ndarray, y: np.ndarray,
               tau: float, iters: int = 8) -> dict[Any, tuple[float, float, int]]:
    """Artıklar: y ~ logit0 + r_a − r_b, r ~ N(0, τ²) (Bayes'çi ridge, köşegen Newton).

    n küçükken r ≈ 0 (ön bilgi), n büyüdükçe veri baskın. Varyans = 1 / (Σ p(1−p) + 1/τ²).
    """
    ids = sorted(set(keys_a) | set(keys_b), key=str)
    idx = {k: i for i, k in enumerate(ids)}
    ia = np.array([idx[k] for k in keys_a])
    ib = np.array([idx[k] for k in keys_b])
    r = np.zeros(len(ids))
    prec0 = 1 / tau ** 2
    h = np.full(len(ids), prec0)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(logit0 + r[ia] - r[ib])))
        g = np.bincount(ia, y - p, len(ids)) - np.bincount(ib, y - p, len(ids)) - prec0 * r
        h = np.bincount(ia, p * (1 - p), len(ids)) + np.bincount(ib, p * (1 - p), len(ids)) + prec0
        r += g / h
    n = np.bincount(ia, minlength=len(ids)) + np.bincount(ib, minlength=len(ids))
    return {k: (float(r[i]), float(1 / h[i]), int(n[i])) for k, i in idx.items()}


def fit_model(du: pl.DataFrame, feats: Callable[[str, str], PlayerFeatures], *,
              use: tuple[bool, ...] = DEFAULT_USE, base: str = "none", base_prior: float = BASE_PRIOR,
              demo: bool = False, line: bool = False, ridge: float = RIDGE,
              tau_demo: float = TAU_DEMO, tau_line: float = TAU_LINE) -> RatingModel:
    """Düellolardan (`duels.duels()` çıktısı) model öğrenir.

    `feats(match_id, steam_id)`: oyuncunun O MAÇTAN ÖNCEKİ FACEIT verisiyle özellikleri
    (geleceğin verisi sızmasın). Varsayılanlar zaman bölmeli sınamada seçilenlerdir.
    """
    base_t, band = fit_base(du, base, base_prior)
    a_ids, b_ids, X, off, y, lines = [], [], [], [], [], []
    for r in du.iter_rows(named=True):
        # A = T tarafındaki oyuncu; y = T kazandı
        t_won = r["winner_side"] == "T"
        a, b = (r["winner"], r["loser"]) if t_won else (r["loser"], r["winner"])
        fa, fb = feats(r["match_id"], a), feats(r["match_id"], b)
        a_ids.append(a)
        b_ids.append(b)
        X.append(_pick(fa.x, use) - _pick(fb.x, use))
        off.append(base_t.get(_line_key(r["line"]), base_t[OTHER]))
        y.append(1.0 if t_won else 0.0)
        lines.append(r["line"])
    n_feat = sum(use)
    off_a, y_a = np.array(off, dtype=float), np.array(y, dtype=float)
    X_a = np.array(X, dtype=float).reshape(len(y), n_feat)
    if n_feat and len(y):
        beta, cov = _fit_logistic(X_a, off_a, y_a, ridge)
    else:
        beta, cov = np.zeros(n_feat), np.eye(n_feat) / ridge
    model = RatingModel(base_t, beta, cov, use, base_band=band, n_duels=len(y))
    logit0 = off_a + X_a @ beta
    if demo and len(y):
        model.delta = _residuals(a_ids, b_ids, logit0, y_a, tau_demo)
        logit0 = logit0 + np.array([model.delta[a][0] - model.delta[b][0]
                                    for a, b in zip(a_ids, b_ids, strict=True)])
    if line and len(y):
        on = [i for i, ln in enumerate(lines) if ln]
        if on:
            model.lam = _residuals([(a_ids[i], lines[i], "T") for i in on],
                                   [(b_ids[i], lines[i], "CT") for i in on], logit0[on], y_a[on], tau_line)
    return model


# --- 3b. Silah etkisi (OPSİYONEL, varsayılan KAPALI: profesör onayı bekliyor) -----------------
# 18 Dust2 demosu, ileri-yönlü sınama (6/9/12/15 maç eğitim, sonraki 3 maç test; 1.999 test
# düellosu, maç-kümeli %90): ELO tabanına göre log-kayıp farkı  tabanca-vs-tüfek −0,013
# [−0,017; −0,008], + AWP uzak/yakın ayrımı −0,017 [−0,022; −0,011]. Mesafe dilimi tek başına
# (+0,000 [−0,001; +0,001]) ve peek / tutma (−0,001 [−0,002; +0,001]) katkı vermedi → yok.
# Girdi: iki oyuncunun O ROUND tuttuğu ana silahın sınıfı (konum kayıtlarından; kill'den ÖNCE
# bilinir, sonuçtan sızmaz) ve düello mesafesi.
WEAPON_FAR = 1100.0       # "uzak" dilim sınırı (duels.DIST_BANDS ile aynı)
WEAPON_FEATURES = ("tabanca", "awp_uzak", "awp_yakın")


def weapon_x(cls_a: str, cls_b: str, dist: float) -> np.ndarray:
    """A için silah özellikleri (A − B farkı): [tabanca, AWP×uzak, AWP×yakın/orta]."""
    far = 1.0 if dist >= WEAPON_FAR else 0.0
    d_pistol = float(cls_a == "pistol") - float(cls_b == "pistol")
    d_awp = float(cls_a == "awp") - float(cls_b == "awp")
    return np.array([d_pistol, d_awp * far, d_awp * (1 - far)])


@dataclass(frozen=True)
class WeaponEffect:
    """Silah sınıfı farkının logit etkisi: katsayılar (WEAPON_FEATURES sırası), kovaryans, n."""

    beta: np.ndarray
    cov: np.ndarray
    n: int


def fit_weapon_effect(cls_t: Iterable[str], cls_ct: Iterable[str], dist: Iterable[float],
                      t_won: Iterable[float], offset: Iterable[float], ridge: float = 1.0
                      ) -> WeaponEffect:
    """Silah etkisini öğrenir. `offset` = silahsız modelin T lehine logit'i (taban + β·Δ)."""
    X = np.array([weapon_x(a, b, d) for a, b, d in zip(cls_t, cls_ct, dist, strict=True)],
                 dtype=float).reshape(-1, len(WEAPON_FEATURES))
    y, off = np.array(list(t_won), dtype=float), np.array(list(offset), dtype=float)
    if len(y) == 0:
        return WeaponEffect(np.zeros(3), np.eye(3) / ridge, 0)
    beta, cov = _fit_logistic(X, off, y, ridge)
    return WeaponEffect(beta, cov, len(y))



# --- 4. Tahmin --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Matchup:
    """A'nın B'yi yenme olasılığı, %90 aralığı, güven etiketi ve dayandığı örnekler."""

    p: float
    lo: float
    hi: float
    conf: str                     # "belirgin" / "belirsiz" / "az veri"
    base: float                   # oyuncu bilgisi olmadan (hat / taraf tabanı)
    n: dict[str, int]             # kaynak örnek sayıları


def _residual_part(model: RatingModel, steam_id: str, line: str | None, side: str
                   ) -> tuple[float, float, int, int]:
    """(artık, varyans, demo düello sayısı, hat düello sayısı) — kapalıysa sıfır."""
    r, v, dn, ln = 0.0, 0.0, 0, 0
    if model.delta:
        r, v, dn = model.delta.get(steam_id, (0.0, TAU_DEMO ** 2, 0))
    if model.lam and line:
        lr, lv, ln = model.lam.get((steam_id, line, side), (0.0, TAU_LINE ** 2, 0))
        r, v = r + lr, v + lv
    return r, v, dn, ln


def matchup(model: RatingModel, a: str, side_a: str, b: str, line: str | None,
            fa: PlayerFeatures, fb: PlayerFeatures, band: str | None = None, *,
            weapon: WeaponEffect | None = None, cls_a: str | None = None,
            cls_b: str | None = None, dist: float | None = None) -> Matchup:
    """P(A, B'yi yener): A `side_a` tarafında, `line` hattında (None = hat dışı).

    `band` sadece model base="line" ile kurulduysa ve o hat × dilim varsa tabanı değiştirir.
    `weapon` (opsiyonel, varsayılan kapalı): silah sınıfları (`cls_a`, `cls_b`) ve mesafe
    (`dist`) da verilirse silah etkisi eklenir; biri eksikse hiç eklenmez.
    """
    key = _line_key(line)
    base_t = model.base_band.get((key, band)) if band else None
    if base_t is None:
        base_t = model.base_t.get(key, model.base_t.get(OTHER, 0.0))
    base = base_t if side_a == "T" else -base_t
    g = _pick(fa.x, model.use) - _pick(fb.x, model.use)
    delta = float(g @ model.beta) if len(g) else 0.0
    var = 0.0
    if len(g):
        var = float(g @ model.cov @ g) + float(model.beta ** 2 @ (_pick(fa.var, model.use) + _pick(fb.var, model.use)))
    side_b = "CT" if side_a == "T" else "T"
    ra, va, dna, lna = _residual_part(model, a, line, side_a)
    rb, vb, dnb, lnb = _residual_part(model, b, line, side_b)
    delta += ra - rb
    var += va + vb
    if weapon is not None and cls_a is not None and cls_b is not None and dist is not None:
        wx = weapon_x(cls_a, cls_b, dist)
        delta += float(wx @ weapon.beta)
        var += float(wx @ weapon.cov @ wx)
    sd = math.sqrt(max(var, 0.0))
    p = _sigmoid(base + bound(delta))
    lo = _sigmoid(base + bound(delta - Z90 * sd))
    hi = _sigmoid(base + bound(delta + Z90 * sd))
    if not (fa.elo_known and fb.elo_known) or min(fa.n_matches, fb.n_matches) < MIN_MATCHES:
        conf = "az veri"
    elif lo > _sigmoid(base) or hi < _sigmoid(base):
        conf = "belirgin"
    else:
        conf = "belirsiz"
    n = {"faceit_maç_a": fa.n_matches, "faceit_maç_b": fb.n_matches, "demo_düello_a": dna,
         "demo_düello_b": dnb, "hat_düello_a": lna, "hat_düello_b": lnb, "model_düello": model.n_duels}
    return Matchup(p, lo, hi, conf, _sigmoid(base), n)


def rating_score(model: RatingModel, f: PlayerFeatures, steam_id: str | None = None
                 ) -> tuple[float, float, float]:
    """Okunaklı puan (0-100): popülasyon ortalamasındaki bir rakibe karşı beklenen düello
    kazanma yüzdesi (taban %50). Döner (puan, alt, üst), %90 aralık; [20, 80] dışına çıkmaz."""
    x = _pick(f.x, model.use)
    th = float(x @ model.beta) if len(x) else 0.0
    var = (float(x @ model.cov @ x) + float(model.beta ** 2 @ _pick(f.var, model.use))) if len(x) else 0.0
    if steam_id is not None:
        r, v, _, _ = _residual_part(model, steam_id, None, "T")
        th, var = th + r, var + v
    sd = math.sqrt(max(var, 0.0))
    return (100 * _sigmoid(bound(th)), 100 * _sigmoid(bound(th - Z90 * sd)),
            100 * _sigmoid(bound(th + Z90 * sd)))


# --- 5. Sınama ölçüleri -----------------------------------------------------------------------


def log_loss(p: Iterable[float], y: Iterable[float]) -> float:
    """Ortalama log-kayıp (düşük iyi; yazı-tura = ln 2 ≈ 0,693)."""
    ps = np.clip(np.array(list(p), dtype=float), 1e-6, 1 - 1e-6)
    ys = np.array(list(y), dtype=float)
    return float(-np.mean(ys * np.log(ps) + (1 - ys) * np.log(1 - ps)))


def brier(p: Iterable[float], y: Iterable[float]) -> float:
    """Ortalama kare hata (düşük iyi; yazı-tura = 0,25)."""
    ps, ys = np.array(list(p), dtype=float), np.array(list(y), dtype=float)
    return float(np.mean((ps - ys) ** 2))


def calibration(p: Iterable[float], y: Iterable[float], bins: int = 5) -> list[dict[str, float]]:
    """Eşit sayılı dilimler: her dilimde ortalama tahmin vs gerçekleşen oran."""
    ps, ys = np.array(list(p), dtype=float), np.array(list(y), dtype=float)
    order = np.argsort(ps, kind="stable")
    return [{"n": len(c), "pred": float(ps[c].mean()), "obs": float(ys[c].mean())}
            for c in np.array_split(order, bins) if len(c)]


# --- 6. Canlı kullanım: FACEIT'ten yükleme ------------------------------------------------------


@dataclass
class RatingContext:
    """Taktik tahtası için hazır model + oyuncu özellikleri (steam_id → PlayerFeatures)."""

    model: RatingModel
    pop: Population
    features: dict[str, PlayerFeatures]
    nicknames: dict[str, str] = field(default_factory=dict)

    def matchup(self, a: str, side_a: str, b: str, line: str | None = None,
                band: str | None = None) -> Matchup | None:
        """steam_id'lerle karşılaşma; özelliği yüklenmemiş oyuncu varsa None."""
        fa, fb = self.features.get(a), self.features.get(b)
        if fa is None or fb is None:
            return None
        return matchup(self.model, a, side_a, b, line, fa, fb, band)


def build_context(du: pl.DataFrame, client: Any, roster: Iterable[Mapping[str, Any]],
                  current: Iterable[Mapping[str, Any]] = ()) -> RatingContext:
    """Model + oyuncu özelliklerini FACEIT'ten kurar (önbellekli; anahtar sadece .env'de).

    roster: düellolardaki maçların kadroları; her satır {match_id, steam_id, faceit_id, level,
    started_at, finished_at} (maç detayından). Eğitim özellikleri maçtan ÖNCEKİ geçmişten.
    current: tahmin edilecek oyuncular {steam_id, faceit_id}; güncel ELO + son 100 maç.
    """
    hist: dict[tuple[str, str], tuple[History, int | None]] = {}
    for r in roster:
        items = client.get_player_match_stats(r["faceit_id"], to_ms=(int(r["finished_at"]) + 60) * 1000)
        ids = [it.get("Match Id") for it in items]
        prior = items[ids.index(r["match_id"]) + 1:] if r["match_id"] in ids else \
            [it for it in items if (it.get("Match Finished At") or 0) < int(r["started_at"]) * 1000]
        hist[(r["match_id"], r["steam_id"])] = (summarize_history(prior), r.get("level"))
    pop = fit_population((h, level_elo(lv)[0]) for h, lv in hist.values())
    cache: dict[tuple[str, str], PlayerFeatures] = {}

    def feats(mid: str, sid: str) -> PlayerFeatures:
        if (mid, sid) not in cache:
            h, lv = hist.get((mid, sid), (History((), ()), None))
            cache[(mid, sid)] = player_features(h, pop, level=lv)
        return cache[(mid, sid)]

    model = fit_model(du, feats)
    out: dict[str, PlayerFeatures] = {}
    names: dict[str, str] = {}
    for c in current:
        prof = client.get_player_live(c["faceit_id"])   # ELO her zaman canlı (≤60 sn), 1 saatlik değil
        game = (prof.get("games") or {}).get("cs2") or {}
        items = client.get_player_match_stats(c["faceit_id"])
        out[c["steam_id"]] = player_features(summarize_history(items), pop, elo=game.get("faceit_elo"),
                                             level=game.get("skill_level"))
        names[c["steam_id"]] = prof.get("nickname", c["steam_id"])
    return RatingContext(model, pop, out, names)


# --- 7. Siteye katılmamış oyuncu: FACEIT geçmişinden puan -------------------------------------

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


@dataclass(frozen=True)
class FaceitRating:
    """FACEIT'ten çözülen oyuncu: kimlik (player_id geri döner) + özellikler."""

    player_id: str
    nickname: str
    steam_id: str | None
    level: int | None
    features: PlayerFeatures


def features_from_faceit(client: Any, who: str, pop: Population, *, kappa: float = KAPPA
                         ) -> FaceitRating:
    """Nickname ya da player_id (UUID) → profil → CANLI ELO + son 100 maç → `player_features`.

    Benzer adlı hesaba düşmemek için nickname, FACEIT'in döndürdüğü profildeki adla TAM (büyük /
    küçük harf dahil) eşleşmezse reddedilir (ValueError); kimlik çözüldükten sonra ELO
    `get_player_live` ile (≤60 sn eski), geçmiş o player_id ile alınır. Bulunamayan nickname
    `FaceitNotFound` fırlatır. Az maçta (ör. 5) puan popülasyon beklentisine (seviye) yaslanır:
    w küçük, stat eki ≈ 0, aralık geniş.
    """
    who = (who or "").strip()
    if not who:
        raise ValueError("Nickname ya da player_id boş.")
    if _UUID.match(who):
        pid = who.lower()
    else:
        prof = client.get_player_by_nickname(who)
        if prof.get("nickname") != who or not prof.get("player_id"):
            raise ValueError(f"'{who}' ile tam eşleşen hesap yok (FACEIT: '{prof.get('nickname')}').")
        pid = prof["player_id"]
    live = client.get_player_live(pid)
    if str(live.get("player_id", pid)).lower() != pid.lower():
        raise ValueError("FACEIT profili istenen player_id ile uyuşmuyor.")
    game = (live.get("games") or {}).get("cs2")
    if not game:
        raise ValueError(f"'{live.get('nickname', who)}' için CS2 profili yok.")
    elo = _num(game.get("faceit_elo"))
    level = int(game["skill_level"]) if game.get("skill_level") else None
    hist = summarize_history(client.get_player_match_stats(pid))
    feats = player_features(hist, pop, elo=elo if elo and elo > 0 else None, level=level, kappa=kappa)
    return FaceitRating(pid, live.get("nickname", who), live.get("steam_id_64"), level, feats)
