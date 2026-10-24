"""Oyuncu kartı: tek güç + genel puan (seviye ölçeği) + silah kayıtları (küçültülmüş) + betimleyiciler.

TEK GÜÇ (kullanıcı kararı 24.10: "tüm güçler harmanlanarak tek güç"): oyuncunun ortalama bir
rakibe karşı beklenen düello gücünün ELO ölçeğinde karşılığı,
    güç = ELO + Σ_k (100·γ_k/β)·z_k        (toplam katkı ±300 ELO ile sınırlı)
düello modeli: logit P(T kazanır) = β·ΔELO/100 + sade silah + Σ_k γ_k·Δz_k. z_k = bileşenin
(FACEIT istatistik eki, demo düello artığı, açılış, trade, clutch, öldürmeden hasar) küçültülmüş,
standartlaştırılmış değeri; γ_k düellolardan ridge ile öğrenilir (elle ağırlık yok), özellikler
bir-maç-dışarıda çapraz-uydurmalı. Oyuncular arası fark ölçülemeyen bileşen (tau² ≤ gürültü) kapalıdır.
Silah kayması tek güce girmez (silaha bağlı; silah-bağımsız ortalaması "düello artığı"dır).
SINAMA SONUCU (18 Dust2 maçı, 2.607 düello, scripts/eval_power.py): tek güç, arenadaki ana
olasılığa (ELO + sade silah) göre log-kayıp kazancı ileri −0,0003 [−0,0026; +0,0017], bir-maç-dışarıda
+0,0010 [−0,0005; +0,0025] → alt sınır > 0 değil: `POWER_VALIDATED = False`, hiçbir bileşen
geçmedi. Güç kartta "doğrulanmadı" gösterilir; ARENA OLASILIĞINA GİRMEZ (`rating.matchup` ayrı).

Seviye ölçeği (tek, sabit, havuzdan bağımsız): ELO → 0-100, FACEIT seviye sınırlarından parçalı
doğrusal. Seviye L'nin ELO aralığı [10·(L−1), 10·L] puana düşer (seviye 9 = 80-90, seviye 10 =
90-100; 100 ELO = 0, 2401+ = 100). DİKKAT: ölçek doğrusal DEĞİL (eğim seviyeye göre 220-400 ELO
/ 10 puan); puan farkı güç oranı gibi okunmamalı (80 puan, 40 puanın "iki katı güçlü" değildir),
yalnız sıralama ve seviye içi konum içindir. Genel puan = canlı FACEIT ELO'su (κ=0; arenadaki ana
düello olasılığıyla aynı girdi, profesör kararı 24.10) ve belirsizliği; birleşik ELO (stat ekli,
kanıtı zayıf) `general.combined_elo` alanında ayrıca verilir.

Küçültme (kullanıcı kararı 24.10: "yetersiz veride ortalama değer göster, yetersizliği ayrıntılı
söyle"): silah ve betimleyici satırlarında değer = ortalama + w·(ham − ortalama), w = n/(n+m),
m momentler yöntemiyle veriden (`fit_prior`); aralık = ±z90·tau·sqrt(1−w). n < 20 "yetersiz veri",
20-49 "az veri", ≥ 50 "yeterli"; `detail` kişisel veri ağırlığını söyler. Silah `validated` her zaman
False: silah yeteneği kayması sınamayı geçmedi (g'ye göre log-kayıp ileri −0,0001 [−0,0007; +0,0004],
bir-maç-dışarıda −0,0001 [−0,0008; +0,0007]). Peek / tutma kartta yok.

Veri sözleşmesi (`PlayerCard.to_dict()`), arayüz bunu kullanır:
    {"nickname": str|null, "steam_id": str, "faceit_level": int|null, "elo": float|null,
     "general": {"score": 0-100|null, "lo": .., "hi": .., "conf": "veri yok"|"az veri"|"yeterli",
                 "combined_elo": float|null, "scale": "seviye ölçeği"},
     "power": null | {"elo": float, "lo_elo": float, "hi_elo": float, "score": 0-100, "lo": .., "hi": ..,
                 "conf": "az veri"|"yeterli", "scale": "seviye ölçeği", "validated": false,
                 "formula": "ELO 1885 +18 faceit istatistik eki … = 1882",
                 "components": [{"key": "elo"|"stat"|"duel"|"opening"|"trade"|"clutch"|"support",
                     "name": str, "contribution_elo": float (ELO için ELO'nun kendisi; toplam = elo),
                     "weight": γ|null, "elo_per_sd": float|null, "z": float|null,
                     "data_weight": 0-1|null, "n": int, "tested": bool}]},
     "weapons": {"rifle"|"awp"|"pistol"|"scout": {"wins": int, "n": int, "lo": 0-1, "hi": 0-1,
                 "label": "yetersiz veri"|"az veri"|"yeterli", "validated": false,
                 "value": 0-1 (küçültülmüş), "raw": 0-1|null, "weight": 0-1, "detail": str}},
     "descriptive": {"opening_win"|"trade_kill"|"clutch_win"|"support_dmg_per_round":
                 {"value": .. (küçültülmüş), "lo": .., "hi": .., "n": int, "label": str,
                  "raw": ..|null, "weight": 0-1, "detail": str}},
     "counts": {"demos": int, "rounds": int, "duels": int}}
Sınır: kayma modeli sadece kill'le biten düellolardan; silah seçimi seçilimlidir; sınama ELO'su
seviye ortasıdır (canlı ELO değil), 18 maç ve 12 test maçıyla küçük örnek.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from squadmind.analysis import rating as rt

# --- Ölçek -------------------------------------------------------------------------------------

# (ELO, puan) kırılma noktaları: FACEIT seviye alt sınırları (rating.LEVEL_ELO_RANGE) + tavan
_SCALE_X = [100.0] + [float(rt.LEVEL_ELO_RANGE[lv][0]) for lv in range(2, 11)] + [2401.0]
_SCALE_Y = [10.0 * i for i in range(11)]
_SCALE_X[1] = 501.0   # seviye 2 alt sınırı 501 (1: 100-500)


def elo_to_score(elo: float) -> float:
    """ELO → 0-100 (sabit parçalı doğrusal ölçek; monoton artan, [0, 100]'e kırpılı)."""
    return float(np.interp(elo, _SCALE_X, _SCALE_Y))


# --- Eşikler -----------------------------------------------------------------------------------

MIN_N = 20        # altında "veri yok"
OK_N = 50         # altında "az veri", üstü "yeterli"

# Kartta gösterilen silah yetenekleri; smg ve ağır silah modelde "smg" altında toplanır (kartta yok)
SHOWN_SKILLS = ("rifle", "awp", "pistol", "scout")
SKILL_NAMES = {"rifle": "Tüfek", "awp": "AWP", "pistol": "Tabanca", "scout": "Scout"}
_GROUP = {"heavy": "smg"}

SKILL_VALIDATED = False   # silah kayması sınamayı geçmedi (modül başlığı); arayüz bayrağı

TAU_G = 0.15      # oyuncunun genel artığı (rating.TAU_DEMO ile aynı)
TAU_D = 0.20      # oyuncu × silah kayması ön bilgi sapması (logit); sınamadan ÖNCE sabitlendi


def label_of(n: int) -> str:
    """Örnek sayısından güven etiketi."""
    return "veri yok" if n < MIN_N else ("az veri" if n < OK_N else "yeterli")


def _group(cls: str | None) -> str:
    c = cls or "pistol"
    return _GROUP.get(c, c)


def label_shrunk(n: float) -> str:
    """Küçültülmüş değerin etiketi (kullanıcı kararı 24.10: veri azsa "veri yok" değil, ortalamaya
    çekilmiş değer + "yetersiz veri")."""
    return "yetersiz veri" if n < MIN_N else ("az veri" if n < OK_N else "yeterli")


# --- Empirik Bayes küçültme (kart satırları ve tek güç bileşenleri ortak kullanır) ---------------

MAX_M = 2000.0        # ön bilginin en büyük "veri birimi" gücü (tau → 0 iken sonsuz olmasın)
TAU_FLOOR = 0.02      # tau tabanı (ortalamaya göre bağıl): her oyuncu aynı sayılmasın diye değil, 0'a bölünmesin diye


@dataclass(frozen=True)
class Prior:
    """Bir oranın / hızın popülasyon ön bilgisi: ortalama mu, gerçek yetenek sapması tau, ön bilgi
    gücü m (payda biriminde; w = n / (n + m))."""

    mu: float
    tau: float
    m: float

    def shrink(self, num: float, den: float) -> tuple[float, float]:
        """(küçültülmüş değer, kişisel veri ağırlığı w). den = 0 → (mu, 0)."""
        if den <= 0:
            return self.mu, 0.0
        w = den / (den + self.m)
        return self.mu + w * (num / den - self.mu), w

    @property
    def flat(self) -> bool:
        """True: bu veride oyuncular arası gerçek fark ölçülemedi (tau² ≤ gürültü; m tavanda) →
        herkes ortalamaya yakın, bileşen z'si 0 sayılır."""
        return self.m >= MAX_M

    def interval(self, w: float) -> float:
        """Gerçek yeteneğin %90 yarı genişliği: z90·tau·sqrt(1−w)."""
        return rt.Z90 * self.tau * math.sqrt(max(1.0 - w, 0.0))


def fit_prior(by_player: Mapping[str, list[tuple[float, float]]], *, binom: bool = True) -> Prior:
    """Oyuncu başına (pay, payda) listelerinden momentler yöntemiyle ön bilgi.

    E[d_i (r_i − mu)²] = s² + d_i·tau² → tau² = (Σ d_i (r_i − mu)² − k·s²) / Σ d_i. Oran için
    s² = mu(1−mu) (Bernoulli); sürekli hızda (ör. destek hasarı / round) s², oyuncu içi maçtan maça
    varyanstan (payda ağırlıklı). Ön bilgi yoksa (veri < 2 oyuncu) m = MAX_M: herkes ortalama.
    """
    tot = {p: (sum(n for n, _ in v), sum(d for _, d in v)) for p, v in by_player.items()}
    items = [(n, d) for n, d in tot.values() if d > 0]
    sd = sum(d for _, d in items)
    if len(items) < 2 or sd <= 0:
        mu = sum(n for n, _ in items) / sd if sd > 0 else 0.0
        return Prior(mu, max(TAU_FLOOR * abs(mu), 1e-6), MAX_M)
    mu = sum(n for n, _ in items) / sd
    if binom:
        s2 = mu * (1 - mu)
    else:
        ss, dof = 0.0, 0
        for p, v in by_player.items():
            vv = [(n, d) for n, d in v if d > 0]
            if len(vv) >= 2:
                r = tot[p][0] / tot[p][1]
                ss += sum(d * (n / d - r) ** 2 for n, d in vv)
                dof += len(vv) - 1
        s2 = ss / dof if dof else mu * mu
    ss_obs = sum(d * (n / d - mu) ** 2 for n, d in items)
    floor = (TAU_FLOOR * max(abs(mu), 1e-3)) ** 2
    tau2 = max((ss_obs - len(items) * s2) / sd, floor)
    return Prior(mu, math.sqrt(tau2), min(s2 / tau2, MAX_M))


# --- Silah kayması modeli ----------------------------------------------------------------------


@dataclass
class CardModel:
    """Düellolardan öğrenilen: ELO eğimi, silah (sade) düzeltmesi, oyuncu artığı ve silah kayması."""

    beta: float                                   # logit / 100 ELO
    weapon: rt.WeaponEffect
    g: dict[str, tuple[float, float, int]] = field(default_factory=dict)            # oyuncu → (r, var, n)
    d: dict[tuple[str, str], tuple[float, float, int]] = field(default_factory=dict)  # (oyuncu, sınıf) → (r, var, n)
    wins: dict[tuple[str, str], int] = field(default_factory=dict)                  # (oyuncu, sınıf) → galibiyet
    n_duels: int = 0


def _prep(du: pl.DataFrame, elo_of: Mapping[tuple[str, str], float]) -> dict[str, list]:
    """Düelloları T-oyuncusu (A) ve CT-oyuncusu (B) eksenine çevirir; ELO'su bilinmeyen atlanır."""
    out: dict[str, list] = {k: [] for k in ("a", "b", "ca", "cb", "dist", "y", "dl", "mid", "ga", "gb")}
    for r in du.iter_rows(named=True):
        t_won = r["winner_side"] == "T"
        a, b = (r["winner"], r["loser"]) if t_won else (r["loser"], r["winner"])
        ca, cb = (r["winner_cls"], r["loser_cls"]) if t_won else (r["loser_cls"], r["winner_cls"])
        ea, eb = elo_of.get((r["match_id"], a)), elo_of.get((r["match_id"], b))
        if ea is None or eb is None:
            continue
        out["a"].append(a); out["b"].append(b)
        out["ca"].append(ca or "pistol"); out["cb"].append(cb or "pistol")
        out["ga"].append(_group(ca)); out["gb"].append(_group(cb))
        out["dist"].append(float(r["dist"])); out["y"].append(1.0 if t_won else 0.0)
        out["dl"].append((ea - eb) / rt.ELO_UNIT); out["mid"].append(r["match_id"])
    return out


def fit_card_model(du: pl.DataFrame, elo_of: Mapping[tuple[str, str], float], *,
                   tau_g: float = TAU_G, tau_d: float = TAU_D, use_g: bool = True,
                   use_d: bool = True) -> CardModel:
    """Düello tablosundan (`duels.duels()`) kart modeli.

    1. β: ΔELO (maç anındaki seviye ortası) ile T kazanma logit'i; 2. onaylı sade silah düzeltmesi
    (`rating.fit_weapon_effect(simple=True)`) β·ΔELO ofsetiyle; 3. oyuncu genel artığı g (τ_g);
    4. oyuncu × silah sınıfı kayması d, g çıkarıldıktan sonra (τ_d): "genele göre fark".
    Hepsi sadece verilen (eğitim) düellolardan öğrenilir.
    """
    p = _prep(du, elo_of)
    y, dl = np.array(p["y"], dtype=float), np.array(p["dl"], dtype=float)
    if len(y) == 0:
        return CardModel(0.0, rt.fit_weapon_effect([], [], [], [], []))
    beta, _ = rt._fit_logistic(dl.reshape(-1, 1), np.zeros(len(y)), y, rt.RIDGE)
    b = float(beta[0])
    off = b * dl
    we = rt.fit_weapon_effect(p["ca"], p["cb"], p["dist"], y, off, simple=True)
    wx = np.array([rt.weapon_x(ca, cb, dd, True) @ we.beta for ca, cb, dd
                   in zip(p["ca"], p["cb"], p["dist"], strict=True)])
    logit0 = off + wx
    model = CardModel(b, we, n_duels=len(y))
    if use_g:
        model.g = rt._residuals(p["a"], p["b"], logit0, y, tau_g)
        logit0 = logit0 + np.array([model.g[a][0] - model.g[c][0] for a, c in zip(p["a"], p["b"], strict=True)])
    if use_d:
        model.d = rt._residuals(list(zip(p["a"], p["ga"], strict=True)), list(zip(p["b"], p["gb"], strict=True)),
                                logit0, y, tau_d)
    for a, c, ga, gb, yy in zip(p["a"], p["b"], p["ga"], p["gb"], y, strict=True):
        model.wins[(a, ga)] = model.wins.get((a, ga), 0) + int(yy)
        model.wins[(c, gb)] = model.wins.get((c, gb), 0) + int(1 - yy)
    return model


def predict_logit(model: CardModel, p: dict[str, list], *, use_g: bool, use_d: bool) -> np.ndarray:
    """T lehine logit (taban: β·ΔELO + sade silah; istenirse + g + d), `_prep` çıktısı için."""
    out = []
    for i in range(len(p["y"])):
        z = model.beta * p["dl"][i] + float(rt.weapon_x(p["ca"][i], p["cb"][i], p["dist"][i], True) @ model.weapon.beta)
        if use_g:
            z += model.g.get(p["a"][i], (0.0,))[0] - model.g.get(p["b"][i], (0.0,))[0]
        if use_d:
            z += model.d.get((p["a"][i], p["ga"][i]), (0.0,))[0] - model.d.get((p["b"][i], p["gb"][i]), (0.0,))[0]
        out.append(z)
    return np.array(out)


# --- Kart --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stat:
    """Bir değer + %90 aralık + örnek sayısı + etiket.

    Küçültülmüş satırlarda (`shrunk_stat`) `value` ortalamaya çekilmiş değerdir; `raw` oyuncunun
    ham değeri (veri varsa), `weight` kişisel veri ağırlığı w (0-1), `detail` insan okunur açıklama.
    Veri hiç yoksa `value` = None (yalnız `rate_stat` ve genel puanın "veri yok" yolunda)."""

    value: float | None
    lo: float | None = None
    hi: float | None = None
    n: int = 0
    label: str = "veri yok"
    raw: float | None = None
    weight: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class Weapon:
    """Silah sınıfında düello kaydı: ham (kazanma / n) + ortalamaya çekilmiş değer + %90 aralık.

    `value` = sınıf ortalamasına küçültülmüş kazanma oranı, `raw` ham oran (n > 0 ise), `weight` =
    kişisel veri ağırlığı, `detail` açıklama. validated her zaman False: silah yeteneği sınamayı
    geçmedi (modül başlığı)."""

    wins: int
    n: int
    lo: float | None
    hi: float | None
    label: str
    validated: bool = False
    value: float | None = None
    raw: float | None = None
    weight: float = 0.0
    detail: str = ""


def _detail(n: float, w: float, unit: str) -> str:
    """"13 düello; ortalamaya çekildi, kişisel veri ağırlığı %18" biçiminde açıklama."""
    ni = int(round(n))
    if ni <= 0:
        return f"hiç {unit} yok; popülasyon ortalaması gösteriliyor (kişisel veri ağırlığı %0)"
    return f"{ni} {unit}; ortalamaya çekildi, kişisel veri ağırlığı %{round(100 * w)}"


def shrunk_stat(prior: Prior, num: float, den: float, unit: str, *, rate: bool = True) -> Stat:
    """Küçültülmüş betimleyici: değer = mu + w·(ham − mu), aralık = ±z90·tau·sqrt(1−w).
    Hiç veri olmasa da dolu döner (value = popülasyon ortalaması, weight = 0)."""
    value, w = prior.shrink(num, den)
    h = prior.interval(w)
    lo, hi = value - h, value + h
    lo = max(lo, 0.0)
    if rate:
        hi = min(hi, 1.0)
    detail = _detail(den, w, unit)
    if prior.flat:
        detail += "; bu veride oyuncular arası fark ölçülemiyor"
    return Stat(value, lo, hi, int(den), label_shrunk(den), (num / den if den > 0 else None), w, detail)


def weapon_priors(counts: Mapping[tuple[str, str], tuple[int, int]] | None,
                  classes: Iterable[str] = SHOWN_SKILLS) -> dict[str, Prior]:
    """Silah sınıfı başına kazanma oranı ön bilgisi (tüm oyuncuların düellolarından)."""
    by: dict[str, dict[str, list[tuple[float, float]]]] = {c: {} for c in classes}
    for (sid, cls), (wins, n) in (counts or {}).items():
        if cls in by and n > 0:
            by[cls].setdefault(sid, []).append((float(wins), float(n)))
    return {c: fit_prior(v, binom=True) if v else Prior(0.5, 0.05, MAX_M) for c, v in by.items()}


def weapon_counts(du: pl.DataFrame) -> dict[tuple[str, str], tuple[int, int]]:
    """(oyuncu, silah sınıfı) → (galibiyet, düello). Tüm düellolar (ELO bilinmese de); heavy → smg."""
    out: dict[tuple[str, str], list[int]] = {}
    for r in du.iter_rows(named=True):
        for who, cls, won in ((r["winner"], r["winner_cls"], 1), (r["loser"], r["loser_cls"], 0)):
            c = out.setdefault((who, _group(cls)), [0, 0])
            c[0] += won
            c[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def weapon_record(counts: Mapping[tuple[str, str], tuple[int, int]] | None, steam_id: str, cls: str,
                  priors: Mapping[str, Prior] | None = None) -> Weapon:
    """Bir oyuncunun bir silah sınıfındaki kaydı: ham + sınıf ortalamasına küçültülmüş değer.

    n < 20 de "veri yok" değil: değer sınıf ortalamasına çekilir, etiket "yetersiz veri", `detail`
    kişisel ağırlığı söyler (kullanıcı kararı 24.10). Aralık küçültülmüş değerin %90 aralığıdır."""
    wins, n = (counts or {}).get((steam_id, cls), (0, 0))
    pr = (priors or weapon_priors(counts)).get(cls) or Prior(0.5, 0.05, MAX_M)
    st = shrunk_stat(pr, wins, n, "düello")
    return Weapon(wins, n, st.lo, st.hi, st.label, False, st.value, st.raw, st.weight or 0.0, st.detail)


def wilson(k: float, n: float, z: float = rt.Z90) -> tuple[float, float]:
    """Oran için Wilson %90 aralığı."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(c - h, 0.0), min(c + h, 1.0)


def rate_stat(num: float, den: float) -> Stat:
    """Ham oran (Wilson aralığı); n < 20 → değer yok. Küçültmesiz yardımcı (kart `shrunk_stat` kullanır)."""
    n = int(den)
    if n < MIN_N or den <= 0:
        return Stat(None, n=n, label=label_of(n))
    lo, hi = wilson(num, den)
    return Stat(num / den, lo, hi, n, label_of(n))


# --- (maç, oyuncu) sayımları: betimleyiciler ve tek güç bileşenleri ortak -----------------------

RATE_COMPS = ("opening", "trade", "clutch", "support")
_COUNTS_CACHE: dict[str, Any] = {"key": None, "val": None}


def component_counts(d: Any) -> dict[tuple[str, str], dict[str, tuple[float, float]]]:
    """(maç, oyuncu) → {bileşen: (pay, payda)}: opening (kazanılan / denenen açılış), trade (trade
    kill / kill), clutch (kazanılan / oynanan), support (öldürmeden hasar / round), rounds (round, 0).
    Son sonuç bellekte tutulur (aynı DemoData için tekrar hesaplanmaz)."""
    if _COUNTS_CACHE["key"] == id(d) and _COUNTS_CACHE["val"][0] is d:
        return _COUNTS_CACHE["val"][1]
    from squadmind.analysis import impact
    from squadmind.analysis.player_metrics import player_match_metrics

    pm = player_match_metrics(d)
    ic = impact.impact_counts(d)
    sup = {(r["match_id"], r["steam_id"]): (float(r["support_dmg"]), float(r["rounds"]))
           for r in ic.iter_rows(named=True)}
    out: dict[tuple[str, str], dict[str, tuple[float, float]]] = {}
    for r in pm.iter_rows(named=True):
        k = (r["match_id"], r["steam_id"])
        out[k] = {
            "opening": (float(r["t_ok"] + r["ct_ok"]), float(r["t_ok"] + r["t_od"] + r["ct_ok"] + r["ct_od"])),
            "trade": (float(r["trade_kills"]), float(r["kills"])),
            "clutch": (float(r["clutches_won"]), float(r["clutches_played"])),
            "support": sup.get(k, (0.0, float(r["rounds"]))),
            "rounds": (float(r["rounds"]), 0.0),
        }
    _COUNTS_CACHE["key"], _COUNTS_CACHE["val"] = id(d), (d, out)
    return out


def _by_player(counts: Mapping[tuple[str, str], dict[str, tuple[float, float]]], comp: str,
               matches: set[str] | None = None) -> dict[str, list[tuple[float, float]]]:
    """Bileşen için oyuncu → [(pay, payda)] (maç başına bir çift); `matches` verilirse yalnız o maçlar."""
    by: dict[str, list[tuple[float, float]]] = {}
    for (mid, sid), cc in counts.items():
        if matches is None or mid in matches:
            by.setdefault(sid, []).append(cc[comp])
    return by


# bileşen → (kart anahtarı, birim, oran mı)
_DESC_UNITS = {"opening": ("opening_win", "açılış düellosu", True), "trade": ("trade_kill", "kill", True),
               "clutch": ("clutch_win", "clutch durumu", True),
               "support": ("support_dmg_per_round", "round", False)}


def descriptives(d: Any, steam_id: str) -> tuple[dict[str, Stat], int, int]:
    """Betimleyici sayılar (DemoData'dan): açılış kazanma, trade (kill'in trade payı), clutch,
    öldürmeden destek hasarı / round. Döner (sayılar, yüklü demo, analiz edilen round).

    Sayılar bir oyuncunun tüm yüklü demolarından havuzlanır ve popülasyon ortalamasına küçültülür
    (`shrunk_stat`; veri azsa ortalamaya yakın, etiket "yetersiz veri"); n = payda (açılış
    denemesi, kill, clutch durumu, round)."""
    counts = component_counts(d)
    mine = {k: v for k, v in counts.items() if k[1] == steam_id}
    out: dict[str, Stat] = {}
    for comp, (name, unit, rate) in _DESC_UNITS.items():
        prior = fit_prior(_by_player(counts, comp), binom=rate)
        num = sum(v[comp][0] for v in mine.values())
        den = sum(v[comp][1] for v in mine.values())
        out[name] = shrunk_stat(prior, num, den, unit, rate=rate)
    return out, len(mine), int(sum(v["rounds"][0] for v in mine.values()))


# --- Tek güç -----------------------------------------------------------------------------------
# Tek güç = oyuncunun "ortalama bir rakibe karşı beklenen düello gücü"nün ELO ölçeğinde karşılığı:
#   güç = ELO + Σ_k (100·γ_k/β)·z_k   (toplam ±MAX_STAT_ADJ ile sınırlı)
# düello modeli:  logit P(T kazanır) = β·ΔELO/100 + w·silah_sade + Σ_k γ_k·Δz_k
# z_k = bileşenin standartlaştırılmış, KÜÇÜLTÜLMÜŞ değeri (yetenek sapması tau birimi; veri azsa 0'a
# çekilir). γ_k veriden (ridge, ön bilgi N(0, GAMMA_SD²)) öğrenilir, elle ağırlık yok. Özellikler
# çapraz-uydurmalı: eğitim düellosunun bileşenleri O MAÇ DIŞARIDA bırakılarak hesaplanır (sızıntı yok).
# Silah kayması (oyuncu × silah) tek güce GİRMEZ: silaha bağlı (silah-bağımsız ortalaması zaten
# "düello artığı" bileşenidir); sınamada ayrı satır olarak ölçülür.

POWER_COMPONENTS = ("stat", "duel", "opening", "trade", "clutch", "support")
DESIGN_COLS = (*POWER_COMPONENTS, "weapon")
COMP_NAMES = {"stat": "FACEIT istatistik eki", "duel": "Demo düello artığı", "opening": "Açılış düellosu",
              "trade": "Trade", "clutch": "Clutch", "support": "Öldürmeden hasar",
              "weapon": "Silah kayması"}
GAMMA_SD = 0.15           # γ ön bilgi sapması (logit / tau birimi); sınamadan ÖNCE sabitlendi
MIN_BETA = 0.01           # β bunun altındaysa ELO karşılığı tanımsız → yalnız ELO
# Bileşen sınamayı geçti mi (scripts/eval_power.py; alt sınır > 0 iki bölmede). Sınama sonucuyla güncellenir.
POWER_TESTED: dict[str, bool] = dict.fromkeys(DESIGN_COLS, False)
POWER_VALIDATED = False   # tek güç (tüm bileşenler) tabana göre sınamayı geçti mi


@dataclass
class CompState:
    """Bir eğitim maç kümesinden hesaplanan oyuncu bileşenleri: z değerleri, ağırlıklar, düello modeli."""

    priors: dict[str, Prior]
    shrunk: dict[str, dict[str, tuple[float, float, float]]]   # bileşen → oyuncu → (z, w, payda)
    model: CardModel
    stat_sd: float


def build_state(counts: Mapping[tuple[str, str], dict[str, tuple[float, float]]], matches: set[str],
                du_s: pl.DataFrame, elo_of: Mapping[tuple[str, str], float], stat_sd: float) -> CompState:
    """`matches` kümesindeki maçlardan oyuncu bileşenleri (düello tablosu `du_s` aynı maçlar)."""
    priors: dict[str, Prior] = {}
    shrunk: dict[str, dict[str, tuple[float, float, float]]] = {}
    for comp in RATE_COMPS:
        by = _by_player(counts, comp, matches)
        pr = fit_prior(by, binom=comp != "support")
        priors[comp] = pr
        sh = {}
        for sid, lst in by.items():
            num, den = sum(n for n, _ in lst), sum(d for _, d in lst)
            val, w = pr.shrink(num, den)
            # Oyuncular arası fark ölçülemiyorsa (pr.flat) bileşen kapalı: z = 0 (tau tabanına bölünmesin)
            sh[sid] = (0.0 if pr.flat else (val - pr.mu) / pr.tau, w, den)
        shrunk[comp] = sh
    return CompState(priors, shrunk, fit_card_model(du_s, elo_of), stat_sd)


def _zc(state: CompState, comp: str, sid: str) -> float:
    return state.shrunk[comp].get(sid, (0.0, 0.0, 0.0))[0]


@dataclass
class Design:
    """Düello tasarım matrisi: ΔELO/100, sade silah özellikleri, bileşen farkları Z (DESIGN_COLS sırası)."""

    y: np.ndarray
    dl: np.ndarray
    wx: np.ndarray
    Z: np.ndarray
    mid: np.ndarray


def design(state: CompState, p: dict[str, list], stat_of: Mapping[tuple[str, str], float]) -> Design:
    """`_prep` çıktısı için tasarım; bileşenler `state`ten (yalnız eğitim maçlarından)."""
    n = len(p["y"])
    Z = np.zeros((n, len(DESIGN_COLS)))
    g, dd = state.model.g, state.model.d
    for i in range(n):
        a, b, mid = p["a"][i], p["b"][i], p["mid"][i]
        Z[i, 0] = (stat_of.get((mid, a), 0.0) - stat_of.get((mid, b), 0.0)) / state.stat_sd
        Z[i, 1] = (g.get(a, (0.0,))[0] - g.get(b, (0.0,))[0]) / TAU_G
        for j, c in enumerate(RATE_COMPS):
            Z[i, 2 + j] = _zc(state, c, a) - _zc(state, c, b)
        Z[i, 6] = (dd.get((a, p["ga"][i]), (0.0,))[0] - dd.get((b, p["gb"][i]), (0.0,))[0]) / TAU_D
    wx = np.array([rt.weapon_x(ca, cb, ds, True) for ca, cb, ds in zip(p["ca"], p["cb"], p["dist"], strict=True)]
                  ).reshape(n, 3)
    return Design(np.array(p["y"], dtype=float), np.array(p["dl"], dtype=float), wx, Z, np.array(p["mid"]))


def power_inputs(hist: Mapping[tuple[str, str], tuple[rt.History, int | None]], pop: rt.Population
                 ) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float]]:
    """(maç, oyuncu) → seviye ortası ELO ve FACEIT stat eki (ELO), maçtan ÖNCEKİ geçmişten
    (`rating.RatingContext.hist`). Seviyesi bilinmeyen oyuncu atlanır."""
    elo_of: dict[tuple[str, str], float] = {}
    stat_of: dict[tuple[str, str], float] = {}
    for k, (h, lv) in hist.items():
        e = rt.level_elo(lv)[0]
        if e is None:
            continue
        elo_of[k] = e
        stat_of[k] = rt.player_features(h, pop, level=lv).stat_adj
    return elo_of, stat_of


def stat_scale(stat_of: Mapping[tuple[str, str], float]) -> float:
    """FACEIT stat eklerinin ölçeği (sd; sonuçtan bağımsız, yalnız standartlaştırma için)."""
    v = np.array(list(stat_of.values()), dtype=float)
    return float(v.std()) if len(v) > 1 and v.std() > 1e-9 else 1.0


def crossfit_design(du: pl.DataFrame, counts: Mapping[tuple[str, str], dict[str, tuple[float, float]]],
                    elo_of: Mapping[tuple[str, str], float], stat_of: Mapping[tuple[str, str], float],
                    stat_sd: float) -> Design:
    """Her maçın düelloları için bileşenler O MAÇ DIŞARIDA (kalan maçlardan) hesaplanır: eğitimde
    γ öğrenilirken özellik sonucu görmesin (bir-maç-dışarıda çapraz-uydurma)."""
    mids = sorted(set(du["match_id"].to_list()))
    parts: list[Design] = []
    for m in mids:
        p = _prep(du.filter(pl.col("match_id") == m), elo_of)
        if not p["y"]:
            continue
        state = build_state(counts, set(mids) - {m}, du.filter(pl.col("match_id") != m), elo_of, stat_sd)
        parts.append(design(state, p, stat_of))
    if not parts:
        return Design(*(np.zeros(0),) * 2, np.zeros((0, 3)), np.zeros((0, len(DESIGN_COLS))), np.zeros(0))
    return Design(np.concatenate([x.y for x in parts]), np.concatenate([x.dl for x in parts]),
                  np.vstack([x.wx for x in parts]), np.vstack([x.Z for x in parts]),
                  np.concatenate([x.mid for x in parts]))


def _x(D: Design, cols: Iterable[str]) -> np.ndarray:
    idx = [DESIGN_COLS.index(c) for c in cols]
    return np.column_stack([D.dl, D.wx, D.Z[:, idx]])


def fit_joint(D: Design, cols: Iterable[str], gamma_sd: float = GAMMA_SD) -> tuple[np.ndarray, np.ndarray]:
    """[β, silah(3), γ_cols] ve kovaryansı. β ve silah: rating ile aynı zayıf ridge; γ: 1/gamma_sd²."""
    cols = tuple(cols)
    ridge = np.array([rt.RIDGE, 1.0, 1.0, 1.0] + [1.0 / gamma_sd ** 2] * len(cols))
    return rt._fit_logistic(_x(D, cols), np.zeros(len(D.y)), D.y, ridge)


def predict_joint(D: Design, beta: np.ndarray, cols: Iterable[str]) -> np.ndarray:
    """T lehine logit (arenadaki gibi ±MAX_LOGIT ile sınırlı)."""
    return np.clip(_x(D, cols) @ beta, -rt.MAX_LOGIT, rt.MAX_LOGIT)


@dataclass
class PowerModel:
    """Öğrenilmiş tek güç modeli: β, silah, γ (+ kovaryans) ve oyuncu bileşen durumu."""

    beta: float
    beta_sd: float
    gamma: dict[str, float]
    gamma_cov: np.ndarray                  # cols sırasıyla
    cols: tuple[str, ...]
    state: CompState                       # tüm maçlardan oyuncu bileşenleri (kartta gösterilen)
    n_duels: int
    gamma_sd: float = GAMMA_SD

    def elo_per_sd(self, comp: str) -> float:
        """Bileşenin 1 sd'lik farkının ELO karşılığı (100·γ/β)."""
        return 100.0 * self.gamma.get(comp, 0.0) / self.beta if self.beta >= MIN_BETA else 0.0


def fit_power(du: pl.DataFrame, counts: Mapping[tuple[str, str], dict[str, tuple[float, float]]],
              elo_of: Mapping[tuple[str, str], float], stat_of: Mapping[tuple[str, str], float], *,
              cols: Iterable[str] = POWER_COMPONENTS, gamma_sd: float = GAMMA_SD) -> PowerModel:
    """Düello tablosundan tek güç ağırlıkları (çapraz-uydurmalı özelliklerle) ve tüm maçlardan durum."""
    cols = tuple(cols)
    sd = stat_scale(stat_of)
    D = crossfit_design(du, counts, elo_of, stat_of, sd)
    mids = set(du["match_id"].to_list())
    state = build_state(counts, mids, du, elo_of, sd)
    if len(D.y) == 0:
        return PowerModel(0.0, 0.0, dict.fromkeys(cols, 0.0), np.eye(len(cols)) * gamma_sd ** 2, cols, state, 0, gamma_sd)
    b, cov = fit_joint(D, cols, gamma_sd)
    k = 4
    return PowerModel(float(b[0]), float(math.sqrt(max(cov[0, 0], 0.0))),
                      {c: float(b[k + i]) for i, c in enumerate(cols)}, cov[k:, k:], cols, state,
                      len(D.y), gamma_sd)


@dataclass(frozen=True)
class PowerPart:
    """Tek gücün bir bileşeni: ELO cinsinden katkı, öğrenilen ağırlık, veri ağırlığı, örnek, sınandı mı."""

    key: str
    name: str
    contribution_elo: float
    weight: float | None        # γ (logit / sd); ELO kendisi için None
    elo_per_sd: float | None    # 1 sd'lik farkın ELO karşılığı (100·γ/β)
    z: float | None             # küçültülmüş standart değer
    data_weight: float | None   # kişisel veri ağırlığı w
    n: int
    tested: bool

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "contribution_elo": round(self.contribution_elo, 2),
                "weight": None if self.weight is None else round(self.weight, 4),
                "elo_per_sd": None if self.elo_per_sd is None else round(self.elo_per_sd, 1),
                "z": None if self.z is None else round(self.z, 3),
                "data_weight": None if self.data_weight is None else round(self.data_weight, 3),
                "n": self.n, "tested": self.tested}


@dataclass(frozen=True)
class Power:
    """Tek güç: ELO ölçeği ve 0-100 seviye ölçeği, %90 aralık, bileşenler (toplamı güce eşit)."""

    elo: float
    lo_elo: float
    hi_elo: float
    score: float
    lo: float
    hi: float
    parts: tuple[PowerPart, ...]
    label: str
    validated: bool

    def formula(self) -> str:
        """"ELO 1885 +12 açılış −3 trade …" (|katkı| < 0,5 ELO olanlar yazılmaz)."""
        out = [f"ELO {self.parts[0].contribution_elo:.0f}"]
        for p in self.parts[1:]:
            if abs(p.contribution_elo) >= 0.5:
                out.append(f"{p.contribution_elo:+.0f} {p.name.lower()}")
        return " ".join(out) + f" = {self.elo:.0f}"

    def to_dict(self) -> dict[str, Any]:
        return {"elo": round(self.elo, 1), "lo_elo": round(self.lo_elo, 1), "hi_elo": round(self.hi_elo, 1),
                "score": self.score, "lo": self.lo, "hi": self.hi, "conf": self.label,
                "scale": "seviye ölçeği", "validated": self.validated, "formula": self.formula(),
                "components": [p.to_dict() for p in self.parts]}


def player_power(model: PowerModel | None, steam_id: str, feats: rt.PlayerFeatures, stat_adj: float = 0.0,
                 label: str = "yeterli") -> Power:
    """Oyuncunun tek gücü. `feats` canlı ELO'lu (κ=0) özellikler, `stat_adj` FACEIT stat eki (ELO,
    κ=0,25'li özelliklerden). Model yoksa (ya da β tanımsızsa) güç = ELO. Bileşen katkıları toplam
    ±MAX_STAT_ADJ ile sınırlıdır (oransal kısılır; güç = ELO + Σ katkı her zaman geçerli)."""
    elo = float(feats.elo)
    elo_var = max(float(feats.var[0]), 0.0) * rt.ELO_UNIT ** 2
    parts = [PowerPart("elo", "Canlı FACEIT ELO", elo, None, None, None, None, feats.n_matches, True)]
    var = elo_var
    if model is not None and model.beta >= MIN_BETA:
        st = model.state
        k = 100.0 / model.beta
        raw: dict[str, tuple[float, float, float, int]] = {}   # key → (z, var_z, w, n)
        adj_var = max(feats.combined_sd ** 2 - elo_var, 0.0)
        raw["stat"] = (stat_adj / st.stat_sd, adj_var / st.stat_sd ** 2, feats.weight, feats.n_matches)
        r_g, v_g, n_g = st.model.g.get(steam_id, (0.0, TAU_G ** 2, 0))
        raw["duel"] = (r_g / TAU_G, v_g / TAU_G ** 2, 1.0 - v_g / TAU_G ** 2, n_g)
        for c in RATE_COMPS:
            z, w, den = st.shrunk[c].get(steam_id, (0.0, 0.0, 0.0))
            # Oyuncular arası fark ölçülemeyen bileşen (flat): z sabit 0, belirsizliği yok
            raw[c] = (z, 0.0 if st.priors[c].flat else 1.0 - w, w, int(den))
        use = [c for c in model.cols if c in raw]
        zv = np.array([raw[c][0] for c in use])
        gv = np.array([model.gamma[c] for c in use])
        idx = [model.cols.index(c) for c in use]
        cov = model.gamma_cov[np.ix_(idx, idx)]
        contrib = k * gv * zv
        total = float(contrib.sum())
        capped = rt.bound(total, rt.MAX_STAT_ADJ)
        scale = capped / total if abs(total) > 1e-12 else 1.0
        for c, cv in zip(use, contrib, strict=True):
            z, vz, w, n = raw[c]
            parts.append(PowerPart(c, COMP_NAMES[c], float(cv) * scale, model.gamma[c], model.elo_per_sd(c),
                                   z, w, n, POWER_TESTED.get(c, False)))
        vz = np.array([raw[c][1] for c in use])
        # Var(γ·z) = z²σγ² (ağırlık belirsizliği) + (γ² + σγ²)·var(z) (oyuncu değeri belirsizliği)
        var += k ** 2 * (float(zv @ cov @ zv) + float(np.sum((gv ** 2 + np.diag(cov)) * vz)))
        var += (capped / model.beta) ** 2 * model.beta_sd ** 2
    power = sum(p.contribution_elo for p in parts)
    sd = math.sqrt(max(var, 0.0))
    return Power(power, power - rt.Z90 * sd, power + rt.Z90 * sd, elo_to_score(power),
                 elo_to_score(power - rt.Z90 * sd), elo_to_score(power + rt.Z90 * sd), tuple(parts), label,
                 POWER_VALIDATED)


@dataclass(frozen=True)
class PlayerCard:
    """Bir oyuncunun kartı (dışa `to_dict()` ile verilir)."""

    steam_id: str
    nickname: str | None
    faceit_level: int | None
    elo: float | None                   # FACEIT ELO (kesin değilse None)
    general: Stat                       # canlı ELO'nun seviye ölçeğindeki puanı (0-100; düelloyla aynı girdi)
    combined_elo: float | None
    weapons: dict[str, Weapon]
    descriptives: dict[str, Stat]
    demos: int
    rounds: int
    duels: int
    power: Power | None = None

    def to_dict(self) -> dict[str, Any]:
        """Arayüz sözleşmesi (modül başlığındaki JSON şekli)."""
        g = self.general
        return {
            "nickname": self.nickname, "steam_id": self.steam_id,
            "faceit_level": self.faceit_level, "elo": self.elo,
            "general": {"score": g.value, "lo": g.lo, "hi": g.hi, "conf": g.label,
                        "combined_elo": self.combined_elo,          # stat ekiyle (kanıtı zayıf)
                        "scale": "seviye ölçeği"},
            "power": self.power.to_dict() if self.power else None,
            "weapons": {c: {"wins": w.wins, "n": w.n, "lo": w.lo, "hi": w.hi, "label": w.label,
                            "validated": w.validated, "value": w.value, "raw": w.raw,
                            "weight": w.weight, "detail": w.detail} for c, w in self.weapons.items()},
            "descriptive": {k: {"value": v.value, "lo": v.lo, "hi": v.hi, "n": v.n, "label": v.label,
                                "raw": v.raw, "weight": v.weight, "detail": v.detail}
                            for k, v in self.descriptives.items()},
            "counts": {"demos": self.demos, "rounds": self.rounds, "duels": self.duels},
        }


def _score_stat(elo: float, sd: float, n: int, label: str) -> Stat:
    return Stat(elo_to_score(elo), elo_to_score(elo - rt.Z90 * sd), elo_to_score(elo + rt.Z90 * sd), n, label)


def player_card(steam_id: str, *, feats: rt.PlayerFeatures | None = None,
                counts: Mapping[tuple[str, str], tuple[int, int]] | None = None,
                desc: tuple[dict[str, Stat], int, int] | None = None,
                nickname: str | None = None, level: int | None = None,
                classes: Iterable[str] = SHOWN_SKILLS, power_model: PowerModel | None = None,
                stat_adj: float = 0.0) -> PlayerCard:
    """Oyuncu kartı. Parçalar ayrı verilir (FACEIT'e ulaşılamazsa genel puan "veri yok" kalır):
    `feats` = rating.player_features çıktısı (canlı ELO, κ=0), `counts` = weapon_counts(düellolar),
    `desc` = descriptives, `power_model` = fit_power, `stat_adj` = FACEIT stat eki (ELO).
    Düello olasılığına HİÇ dokunmaz (rating.matchup'tan bağımsız)."""
    power = None
    if feats is None:
        general, comb, elo = Stat(None, label="veri yok"), None, None
    else:
        lab = "az veri" if (not feats.elo_known or feats.n_matches < rt.MIN_MATCHES) else "yeterli"
        # Profesör kararı 24.10 (a): genel puan YALNIZ canlı ELO'dan (κ=0), arenadaki ana düello
        # olasılığıyla aynı girdi; ELO'nun belirsizliği var[0] (kesin ELO'da 0, seviye ortasında
        # seviye aralığı). Birleşik ELO (stat ekli, kanıtı zayıf) ayrı alanda kalır.
        elo_sd = math.sqrt(max(float(feats.var[0]), 0.0)) * rt.ELO_UNIT
        general = _score_stat(feats.elo, elo_sd, feats.n_matches, lab)
        comb = feats.combined_elo
        elo = feats.elo if feats.elo_exact else None
        power = player_power(power_model, steam_id, feats, stat_adj, lab)
    pri = weapon_priors(counts, classes)
    weapons = {c: weapon_record(counts, steam_id, c, pri) for c in classes}
    stats, demos, rounds = desc if desc else ({}, 0, 0)
    n_duels = sum(v[1] for k, v in (counts or {}).items() if k[0] == steam_id)
    return PlayerCard(steam_id, nickname, level, elo, general, comb, weapons, stats, demos, rounds, n_duels,
                      power)
