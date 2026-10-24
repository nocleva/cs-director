"""Oyuncu kartı: genel puan (seviye ölçeği) + silah kayıtları (ham, doğrulanmadı) + betimleyiciler.

Kartta PUAN olarak yalnız genel puan vardır (profesör kararı 24.10). Silah satırlarında puan ve
oyuncu×silah kayması YOKTUR (kayma iç hesapta `fit_card_model`de kalır, dışa verilmez).

Seviye ölçeği (tek, sabit, havuzdan bağımsız): ELO → 0-100, FACEIT seviye sınırlarından parçalı
doğrusal. Seviye L'nin ELO aralığı [10·(L−1), 10·L] puana düşer (seviye 9 = 80-90, seviye 10 =
90-100; 100 ELO = 0, 2401+ = 100). DİKKAT: ölçek doğrusal DEĞİL (eğim seviyeye göre 220-400 ELO
/ 10 puan); puan farkı güç oranı gibi okunmamalı (80 puan, 40 puanın "iki katı güçlü" değildir),
yalnız sıralama ve seviye içi konum içindir. Genel puan = canlı FACEIT ELO'su (κ=0; arenadaki ana
düello olasılığıyla aynı girdi, profesör kararı 24.10) ve belirsizliği; birleşik ELO (stat ekli,
kanıtı zayıf) `general.combined_elo` alanında ayrıca verilir.

Silah satırı: o sınıftaki ham düello (galibiyet / n) + Wilson %90 aralığı; n < 20 "veri yok"
(aralık verilmez), 20-49 "az veri", ≥ 50 "yeterli". `validated` her zaman False
("doğrulanmadı"): silah yeteneği kayması sınamayı geçmedi (18 Dust2 maçı, g'ye göre log-kayıp
ileri −0,0001 [−0,0007; +0,0004], bir-maç-dışarıda −0,0001 [−0,0008; +0,0007]).
Betimleyiciler (açılış kazanma, trade, clutch, öldürmeden hasar/round): n < 20 "veri yok".
Peek / tutma kartta yok. Kart düello olasılığını etkilemez (`rating.matchup` ayrı).

Veri sözleşmesi (`PlayerCard.to_dict()`), arayüz bunu kullanır:
    {"nickname": str|null, "steam_id": str, "faceit_level": int|null, "elo": float|null,
     "general": {"score": 0-100|null, "lo": .., "hi": .., "conf": "veri yok"|"az veri"|"yeterli",
                 "combined_elo": float|null, "scale": "seviye ölçeği"},
     "weapons": {"rifle"|"awp"|"pistol"|"scout": {"wins": int, "n": int, "lo": 0-1|null,
                 "hi": 0-1|null, "label": "veri yok"|"az veri"|"yeterli", "validated": false}},
     "descriptive": {"opening_win"|"trade_kill"|"clutch_win"|"support_dmg_per_round":
                 {"value": ..|null, "lo": ..|null, "hi": ..|null, "n": int, "label": str}},
     "counts": {"demos": int, "rounds": int, "duels": int}}
Sınır: kayma modeli sadece kill'le biten düellolardan; silah seçimi seçilimlidir.
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
class Weapon:
    """Silah sınıfında HAM düello kaydı (kazanma / n) + Wilson %90 aralığı. Puan YOK, kayma YOK.

    validated her zaman False: silah yeteneği sınamayı geçmedi (modül başlığı)."""

    wins: int
    n: int
    lo: float | None
    hi: float | None
    label: str
    validated: bool = False


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

    def to_dict(self) -> dict[str, Any]:
        """Arayüz sözleşmesi (modül başlığındaki JSON şekli)."""
        g = self.general
        return {
            "nickname": self.nickname, "steam_id": self.steam_id,
            "faceit_level": self.faceit_level, "elo": self.elo,
            "general": {"score": g.value, "lo": g.lo, "hi": g.hi, "conf": g.label,
                        "combined_elo": self.combined_elo,          # stat ekiyle (kanıtı zayıf)
                        "scale": "seviye ölçeği"},
            "weapons": {c: {"wins": w.wins, "n": w.n, "lo": w.lo, "hi": w.hi, "label": w.label,
                            "validated": w.validated} for c, w in self.weapons.items()},
            "descriptive": {k: {"value": v.value, "lo": v.lo, "hi": v.hi, "n": v.n, "label": v.label}
                            for k, v in self.descriptives.items()},
            "counts": {"demos": self.demos, "rounds": self.rounds, "duels": self.duels},
        }


def _score_stat(elo: float, sd: float, n: int, label: str) -> Stat:
    return Stat(elo_to_score(elo), elo_to_score(elo - rt.Z90 * sd), elo_to_score(elo + rt.Z90 * sd), n, label)


def weapon_counts(du: pl.DataFrame) -> dict[tuple[str, str], tuple[int, int]]:
    """(oyuncu, silah sınıfı) → (galibiyet, düello). Tüm düellolar (ELO bilinmese de); heavy → smg."""
    out: dict[tuple[str, str], list[int]] = {}
    for r in du.iter_rows(named=True):
        for who, cls, won in ((r["winner"], r["winner_cls"], 1), (r["loser"], r["loser_cls"], 0)):
            c = out.setdefault((who, _group(cls)), [0, 0])
            c[0] += won
            c[1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def weapon_record(counts: Mapping[tuple[str, str], tuple[int, int]] | None, steam_id: str, cls: str) -> Weapon:
    """Bir oyuncunun bir silah sınıfındaki ham kaydı; n < 20 → aralık yok, "veri yok"."""
    wins, n = (counts or {}).get((steam_id, cls), (0, 0))
    if n < MIN_N:
        return Weapon(wins, n, None, None, label_of(n))
    lo, hi = wilson(wins, n)
    return Weapon(wins, n, lo, hi, label_of(n))


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


def player_card(steam_id: str, *, feats: rt.PlayerFeatures | None = None,
                counts: Mapping[tuple[str, str], tuple[int, int]] | None = None,
                desc: tuple[dict[str, Stat], int, int] | None = None,
                nickname: str | None = None, level: int | None = None,
                classes: Iterable[str] = SHOWN_SKILLS) -> PlayerCard:
    """Oyuncu kartı. Parçalar ayrı verilir (FACEIT'e ulaşılamazsa genel puan "veri yok" kalır):
    `feats` = rating.player_features çıktısı, `counts` = weapon_counts(düellolar),
    `desc` = descriptives. Düello olasılığına HİÇ dokunmaz (rating.matchup'tan bağımsız)."""
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
    weapons = {c: weapon_record(counts, steam_id, c) for c in classes}
    stats, demos, rounds = desc if desc else ({}, 0, 0)
    n_duels = sum(v[1] for k, v in (counts or {}).items() if k[0] == steam_id)
    return PlayerCard(steam_id, nickname, level, elo, general, comb, weapons, stats, demos, rounds, n_duels)
