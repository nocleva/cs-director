"""Oyuncu kartı: genel puan + silah yetenekleri + betimleyici sayılar, hepsi n ve güven etiketiyle.

Ölçek (tek, sabit, havuzdan bağımsız): ELO → 0-100, FACEIT seviye sınırlarından parçalı doğrusal.
Seviye L'nin ELO aralığı [10·(L−1), 10·L] puana düşer (seviye 9 = 80-90, seviye 10 = 90-100 ve
2401+ → 100); 100 ELO → 0 puan. Seviye sınırlarında kırılır, aralarında doğrusaldır, tek yönlü
(artan) ve oyuncu havuzundan bağımsızdır: iki ayrı havuzdaki oyuncular aynı ölçekte kıyaslanır.
Neden olasılık ölçeği değil: tek düello β = 0,047 / 100 ELO ile %46-54 arasına sıkışır (ayırt
etmez); havuz-ortalamalı z ölçeği de havuz değişince oyuncunun puanını oynatırdı.

Genel puan = `rating.PlayerFeatures.combined_elo` (FACEIT ELO + küçültmeli stat eki, sınanmış),
%90 aralığı `combined_sd`'den. Silah yeteneği = AYNI ölçekte "genel + o silahtaki kayma":
    kayma_logit = oyuncu×silah artığı d  (hiyerarşik küçültme, bkz. `fit_card_model`)
    kayma_ELO   = 300·tanh( (d / β·100) / 300 )   (β = ELO başına düello logit'i; ±300 ELO sınırı,
                  `rating.MAX_STAT_ADJ` ile aynı gerekçe)
    silah puanı = ölçek(birleşik ELO + kayma_ELO)
Yani "genele göre fark", az düelloda ≈ 0 (genel puana yapışır), düello arttıkça açılır.

Eşikler (düello sayısı n): n < 20 "veri yok" (değer verilmez), 20-49 "az veri", ≥ 50 "yeterli".
"belirgin" ayrıca: kaymanın %90 aralığı 0'ı dışlıyor.

Dürüstlük: silah kayması düello olasılığına GİRMEDEN ÖNCE sınanır (scripts/eval_cards.py).
Sonuç (18 Dust2 maçı, 24.10): kayma ek katkı vermedi (g'ye göre ileri −0,0001 [−0,0007; +0,0004],
bir-maç-dışarıda −0,0001 [−0,0008; +0,0007]) → `SKILL_VALIDATED = False`: silah puanları
BETİMLEYİCİDİR, düello olasılığına girmez ve arayüzde "doğrulanmadı" diye gösterilmelidir. Sınır: kayma sadece kill'le biten
düellolardan (gözlenen bir yan etki: silah seçimi seçilimlidir).
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
MAX_SKILL_SHIFT = rt.MAX_STAT_ADJ   # silah kaymasının ELO karşılığı sınırı
MIN_BETA = 0.01   # logit / 100 ELO; altında ELO'ya çevirme anlamsız (belirsiz)

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
    """Bir değer + %90 aralık + örnek sayısı + etiket. Veri yoksa value = None."""

    value: float | None
    lo: float | None = None
    hi: float | None = None
    n: int = 0
    label: str = "veri yok"


@dataclass(frozen=True)
class Skill:
    """Silah yeteneği: 0-100 puan, genele göre kayma (ELO eşdeğeri ve logit), n, etiket."""

    score: Stat
    shift_elo: float | None
    shift_logit: float | None
    wins: int
    n: int
    clear: bool          # kaymanın %90 aralığı 0'ı dışlıyor mu


@dataclass(frozen=True)
class PlayerCard:
    """Bir oyuncunun kartı."""

    steam_id: str
    nickname: str | None
    general: Stat                       # birleşik ELO'nun 0-100 puanı
    combined_elo: float | None
    duel_pct: Stat                      # ortalama rakibe karşı beklenen düello % (rating_score)
    skills: dict[str, Skill]
    descriptives: dict[str, Stat]
    demos: int
    rounds: int
    duels: int


def _score_stat(elo: float, sd: float, n: int, label: str) -> Stat:
    return Stat(elo_to_score(elo), elo_to_score(elo - rt.Z90 * sd), elo_to_score(elo + rt.Z90 * sd), n, label)


def skill_of(cm: CardModel | None, steam_id: str, cls: str, feats: rt.PlayerFeatures | None) -> Skill:
    """Bir oyuncunun bir silah sınıfındaki yeteneği (genele göre kayma, küçültmeli)."""
    r, var, _ = (cm.d.get((steam_id, cls)) if cm else None) or (0.0, TAU_D ** 2, 0)
    wins = cm.wins.get((steam_id, cls), 0) if cm else 0
    # n: bu sınıftaki düello sayısı (kazanma + kaybetme); küçültme zaten az n'de r ≈ 0 yapar
    n = _count(cm, steam_id, cls) if cm else 0
    lab = label_of(n)
    if cm is None or n < MIN_N:
        return Skill(Stat(None, n=n, label=lab), None, None, wins, n, False)
    sd = math.sqrt(max(var, 0.0))
    clear = abs(r) > rt.Z90 * sd
    if cm.beta < MIN_BETA or feats is None:
        # ELO'ya çevrilemez (β belirsiz) ya da genel puan yok: sadece logit kayması
        return Skill(Stat(None, n=n, label=lab), None, r, wins, n, clear)
    raw = r / cm.beta * rt.ELO_UNIT                          # kayma, ELO eşdeğeri (sınırsız)
    shift = rt.bound(raw, MAX_SKILL_SHIFT)
    slope = 1.0 - math.tanh(raw / MAX_SKILL_SHIFT) ** 2     # sınırın türevi
    shift_sd = slope * sd / cm.beta * rt.ELO_UNIT
    elo = feats.combined_elo + shift
    tot_sd = math.sqrt(feats.combined_sd ** 2 + shift_sd ** 2)
    return Skill(_score_stat(elo, tot_sd, n, lab), shift, r, wins, n, clear)


def _count(cm: CardModel, steam_id: str, cls: str) -> int:
    """Oyuncunun o sınıftaki düello sayısı (`_residuals` n'i: A ya da B olarak geçen düello)."""
    hit = cm.d.get((steam_id, cls))
    return hit[2] if hit else 0


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
    """Oran (pay / payda) → Stat; n = payda."""
    n = int(den)
    if n < MIN_N or den <= 0:
        return Stat(None, n=n, label=label_of(n))
    lo, hi = wilson(num, den)
    return Stat(num / den, lo, hi, n, label_of(n))


def descriptives(d: Any, steam_id: str) -> tuple[dict[str, Stat], int, int]:
    """Betimleyici sayılar (DemoData'dan): açılış kazanma, trade (kill'in trade payı), clutch,
    öldürmeden destek hasarı / round. Döner (sayılar, yüklü demo, analiz edilen round).

    Sayılar bir oyuncunun tüm yüklü demolarından havuzlanır; oranlarda n = payda (açılış denemesi,
    kill, clutch durumu); destek hasarında n = round.
    """
    from squadmind.analysis import impact
    from squadmind.analysis.player_metrics import aggregate, player_match_metrics

    pm = player_match_metrics(d).filter(pl.col("steam_id") == steam_id)
    if pm.height == 0:
        return {}, 0, 0
    ag = aggregate(pm)
    ic = impact.impact_counts(d).filter(pl.col("steam_id") == steam_id)
    rounds = int(ag["rounds"].num)
    opening_w = ag["t_open_win"].num + ag["ct_open_win"].num
    opening_n = ag["t_open_win"].den + ag["ct_open_win"].den
    out = {
        "opening_win": rate_stat(opening_w, opening_n),
        "trade_kill": rate_stat(ag["trade_kill"].num, ag["trade_kill"].den),
        "clutch_win": rate_stat(ag["clutch_win"].num, ag["clutch_win"].den),
    }
    sup = impact.impact_rates(ic)["support_dmg_per_round"] if ic.height else None
    out["support_dmg_per_round"] = (Stat(sup.value, None, None, int(sup.den), label_of(int(sup.den)))
                                    if sup and sup.value is not None and sup.den >= MIN_N
                                    else Stat(None, n=int(sup.den) if sup else 0, label="veri yok"))
    return out, pm.height, rounds


def player_card(steam_id: str, *, rating_model: rt.RatingModel | None = None,
                feats: rt.PlayerFeatures | None = None, card_model: CardModel | None = None,
                desc: tuple[dict[str, Stat], int, int] | None = None,
                nickname: str | None = None, classes: Iterable[str] = SHOWN_SKILLS) -> PlayerCard:
    """Oyuncu kartı. Parçalar ayrı verilir (FACEIT'e ulaşılamazsa genel puan "veri yok" kalır):
    `feats` = rating.player_features çıktısı, `card_model` = fit_card_model, `desc` = descriptives."""
    if feats is None:
        general, comb, pct = Stat(None, label="veri yok"), None, Stat(None, label="veri yok")
    else:
        lab = "az veri" if (not feats.elo_known or feats.n_matches < rt.MIN_MATCHES) else "yeterli"
        general = _score_stat(feats.combined_elo, feats.combined_sd, feats.n_matches, lab)
        comb = feats.combined_elo
        if rating_model is not None:
            s, lo, hi = rt.rating_score(rating_model, feats, steam_id)
            pct = Stat(s, lo, hi, feats.n_matches, lab)
        else:
            pct = Stat(None, label="veri yok")
    skills = {c: skill_of(card_model, steam_id, c, feats) for c in classes}
    stats, demos, rounds = desc if desc else ({}, 0, 0)
    n_duels = 0
    if card_model:
        n_duels = sum(v[2] for k, v in card_model.d.items() if k[0] == steam_id)
    return PlayerCard(steam_id, nickname, general, comb, pct, skills, stats, demos, rounds, n_duels)
