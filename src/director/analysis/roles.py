"""Rol sınıflandırma (CLAUDE.md bölüm 6) — kural tabanlı, takım içi karşılaştırmalı.

FACEIT maçlarında takım arkadaşları değişir; bu yüzden her sinyal önce maç içinde,
oyuncunun kendi takımındaki yerine çevrilir (0 = takımın en düşüğü, 1 = en yükseği).
Rol puanı bu takım içi yüzdeliklerin maçlar boyunca ortalamasıdır. AWPer ise mutlak
eşikle belirlenir: kill'lerin %35+'ı keskin nişancı tüfeğiyle (AWP + Scout). Scout'u
dahil ediyoruz çünkü rolü belirleyen silah değil, oynanan pozisyon ve açı.

Her rol bir güven etiketiyle verilir: az maç ya da zayıf sinyal → düşük güven.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

AWP_THRESHOLD = 0.35

# Güven: örneklem (maç sayısı) ve sinyal gücü (puan) birlikte
MIN_MATCHES_MEDIUM = 5
MIN_MATCHES_HIGH = 12
STRONG_SIGNAL = 0.75
MODERATE_SIGNAL = 0.60

ROLE_NAMES = {
    "entry": "Entry",
    "support": "Support",
    "lurker": "Lurker",
    "awper": "AWPer",
    "anchor": "Anchor",
    "rotator": "Rotator",
}
T_ROLES = ("entry", "support", "lurker", "awper")
CT_ROLES = ("anchor", "rotator", "awper")

# Her rolün hangi sinyallerden oluştuğu (rapor "neden" kısmında gösterir)
ROLE_SIGNALS = {
    "entry": "T'de opening duel'e girme oranı ve erken ilk temas (takım içi sıra)",
    "support": "round başı flash, flash assist + kör edilen rakip ve utility hasarı (takım içi sıra)",
    "lurker": "T'de takım merkezinden uzaklık ve geç ilk temas (takım içi sıra)",
    "awper": f"kill'lerin AWP + Scout ile alınan payı (eşik %{AWP_THRESHOLD * 100:.0f})",
    "anchor": "CT'de aynı site'ı tutma oranı (takım içi sıra)",
    "rotator": "CT'de round içinde site değiştirme oranı (takım içi sıra)",
}


def _team_pct(col: str, higher_is_more: bool = True) -> pl.Expr:
    """Takım içi yüzdelik: 0 = takımın en düşüğü, 1 = en yükseği (eşitlikte ortalama)."""
    v = pl.col(col) if higher_is_more else -pl.col(col)
    rank = v.rank("average").over("match_id", "faction")
    n = pl.len().over("match_id", "faction")
    return pl.when(n > 1).then((rank - 1) / (n - 1)).otherwise(None)


def team_signals(pm: pl.DataFrame) -> pl.DataFrame:
    """Her (maç, oyuncu) için rol sinyallerinin takım içi yüzdelikleri."""
    df = pm.with_columns(
        (pl.col("t_ok") + pl.col("t_od")).truediv(pl.col("t_rounds")).alias("_t_open"),
        pl.col("t_contact_sum").truediv(pl.col("t_contact_n")).alias("_t_contact"),
        pl.col("flashes").truediv(pl.col("rounds")).alias("_flash"),
        (pl.col("flash_assists") + pl.col("flashed_enemies"))
        .truediv(pl.col("rounds")).alias("_flash_fx"),
        pl.col("util_dmg").truediv(pl.col("rounds")).alias("_util_dmg"),
        pl.col("ct_anchor_rounds").truediv(pl.col("ct_setup_rounds")).alias("_anchor"),
        pl.col("ct_rotations").truediv(pl.col("ct_setup_rounds")).alias("_rotation"),
    )
    return df.with_columns(
        pl.mean_horizontal(_team_pct("_t_open"), _team_pct("_t_contact", False)).alias("entry"),
        pl.mean_horizontal(_team_pct("_flash"), _team_pct("_flash_fx"),
                           _team_pct("_util_dmg")).alias("support"),
        pl.mean_horizontal(_team_pct("lurk_dist"), _team_pct("_t_contact")).alias("lurker"),
        _team_pct("_anchor").alias("anchor"),
        _team_pct("_rotation").alias("rotator"),
    ).select("match_id", "steam_id", "entry", "support", "lurker", "anchor", "rotator")


@dataclass
class RoleScore:
    role: str
    score: float | None   # 0-1
    matches: int          # puana katkı veren maç sayısı
    confidence: str       # yüksek / orta / düşük / yok

    @property
    def name(self) -> str:
        return ROLE_NAMES[self.role]


def _confidence(score: float | None, matches: int) -> str:
    if score is None or matches == 0:
        return "yok"
    if score < MODERATE_SIGNAL:
        return "yok"  # rol sinyali yok
    if matches < MIN_MATCHES_MEDIUM:
        return "düşük"
    if score >= STRONG_SIGNAL and matches >= MIN_MATCHES_HIGH:
        return "yüksek"
    return "orta"


def role_scores(pm: pl.DataFrame, steam_id: str) -> dict[str, RoleScore]:
    """Oyuncunun tüm rol puanları."""
    sig = team_signals(pm).filter(pl.col("steam_id") == steam_id)
    out: dict[str, RoleScore] = {}
    for role in ("entry", "support", "lurker", "anchor", "rotator"):
        vals = sig[role].drop_nulls()
        score = float(vals.mean()) if len(vals) else None
        out[role] = RoleScore(role, score, len(vals), _confidence(score, len(vals)))

    # AWPer: mutlak eşik; puan = keskin nişancı payının eşiğe oranı (en fazla 1)
    me = pm.filter(pl.col("steam_id") == steam_id)
    kills = float(me["kills"].sum()) if me.height else 0.0
    share = float(me["k_awp"].sum() + me["k_scout"].sum()) / kills if kills else None
    awp_score = min(1.0, share / AWP_THRESHOLD) if share is not None else None
    awp_conf = "yok" if share is None or share < AWP_THRESHOLD else _confidence(1.0, me.height)
    out["awper"] = RoleScore("awper", awp_score, me.height, awp_conf)
    return out


def side_roles(scores: dict[str, RoleScore]) -> dict[str, list[RoleScore]]:
    """Her taraf için sinyali olan roller, önem sırasıyla (birincil önce).

    AWPer eşiği geçtiyse başa alınır: silah tercihi en net, en az yoruma açık sinyal.
    """
    out: dict[str, list[RoleScore]] = {}
    for side, roles in (("T", T_ROLES), ("CT", CT_ROLES)):
        cands = [scores[r] for r in roles if scores[r].confidence != "yok"]
        cands.sort(key=lambda c: (c.role != "awper", -(c.score or 0)))
        out[side] = cands
    return out


def primary_roles(scores: dict[str, RoleScore]) -> tuple[RoleScore | None, RoleScore | None]:
    """T ve CT tarafı için birincil rol (sinyal yoksa None)."""
    sr = side_roles(scores)
    return (sr["T"][0] if sr["T"] else None, sr["CT"][0] if sr["CT"] else None)
