"""Takım ekonomi kararları (CLAUDE.md bölüm 6, Faz 2a).

Girdi `team_tactics.team_rounds`: oyuncunun takımının her round'u, iki tarafın alım tipi
(pistol / eco / force / full, awpy) ve freeze sonundaki ekipman değeri.

Bir kararın değeri sadece o round'un sonucu değildir: kayıptan sonra eco yapmak o round'u
büyük ihtimalle kaybettirir ama sonraki round'da full buy sağlar. Bu yüzden kayıp sonrası
kararlar "bu round + sonraki round" ile birlikte değerlendirilir.
"""

from __future__ import annotations

import polars as pl

_RK = ["match_id", "round_no"]
BUY_ORDER = {"pistol": 0, "eco": 1, "force": 2, "full": 3}
# MR12: her yarı 12 round; uzatmalar 3'lük yarılar
HALF_ROUNDS, OT_HALF_ROUNDS = 12, 3
# Ekipman farkı aralıkları ($, bizim - rakip)
EQUIP_BINS = [-10000, -3000, 3000, 10000]
EQUIP_LABELS = ["rakip çok önde (>10k$)", "rakip önde (3-10k$)", "denk (±3k$)",
                "biz öndeyiz (3-10k$)", "biz çok öndeyiz (>10k$)"]


def _half(round_no: pl.Expr) -> pl.Expr:
    """Yarı numarası: 1-12 → 0, 13-24 → 1, uzatmalar 3'lük yarılar → 2, 3, ..."""
    ot = (round_no - 2 * HALF_ROUNDS - 1) // OT_HALF_ROUNDS + 2
    return pl.when(round_no <= 2 * HALF_ROUNDS).then((round_no - 1) // HALF_ROUNDS).otherwise(ot)


def with_context(tr: pl.DataFrame) -> pl.DataFrame:
    """Her round'a aynı yarıdaki önceki / sonraki round bilgisi eklenir.

    Eklenen kolonlar: half, prev_won (yarının ilk round'unda boş), next_won, next_buy
    (yarının son round'unda boş)
    """
    t = tr.sort(*_RK).with_columns(_half(pl.col("round_no")).alias("half"))
    by = ["match_id", "half"]
    return t.with_columns(
        pl.col("won").shift(1).over(by).alias("prev_won"),
        pl.col("won").shift(-1).over(by).alias("next_won"),
        pl.col("own_buy").shift(-1).over(by).alias("next_buy"),
    )


def buy_matchups(tr: pl.DataFrame) -> pl.DataFrame:
    """Bizim alım × rakip alım: round ve kazanma (pistol round'ları kendi satırında)."""
    return (
        tr.group_by("own_buy", "enemy_buy")
        .agg(pl.len().alias("rounds"), pl.col("won").sum().alias("wins"))
        .with_columns(pl.col("own_buy").replace_strict(BUY_ORDER, default=9).alias("_o"),
                      pl.col("enemy_buy").replace_strict(BUY_ORDER, default=9).alias("_e"))
        .sort("_o", "_e").drop("_o", "_e")
    )


def pistol_conversion(tr: pl.DataFrame) -> pl.DataFrame:
    """Pistol round sonucu → sonraki iki round (2. ve 3. round) kazanma.

    Kolonlar: pistol_won, halves, r2_wins, r3_wins, r2_force (pistol kaybında 2. round
    force buy sayısı)
    """
    ctx = with_context(tr).with_columns(
        pl.col("won").shift(-2).over("match_id", "half").alias("third_won"))
    pistols = ctx.filter(pl.col("is_pistol"))
    return (
        pistols.group_by(pl.col("won").alias("pistol_won"))
        .agg(pl.len().alias("halves"),
             pl.col("next_won").sum().alias("r2_wins"),
             pl.col("third_won").sum().alias("r3_wins"),
             (pl.col("next_buy") == "force").sum().alias("r2_force"))
        .sort("pistol_won", descending=True)
    )


def after_loss_decisions(tr: pl.DataFrame) -> pl.DataFrame:
    """Round kaybından sonra (pistol hariç) alım kararı: bu round ve sonraki round.

    Kolonlar: own_buy, rounds, wins, next_rounds (yarı içinde sonraki round'u olan),
    next_wins, next_full (sonraki round full buy sayısı)
    """
    ctx = with_context(tr).filter(~pl.col("is_pistol") & (pl.col("prev_won") == False))  # noqa: E712
    return (
        ctx.group_by("own_buy")
        .agg(pl.len().alias("rounds"), pl.col("won").sum().alias("wins"),
             pl.col("next_won").is_not_null().sum().alias("next_rounds"),
             pl.col("next_won").sum().alias("next_wins"),
             (pl.col("next_buy") == "full").sum().alias("next_full"))
        .with_columns(pl.col("own_buy").replace_strict(BUY_ORDER, default=9).alias("_o"))
        .sort("_o").drop("_o")
    )


def equipment_edge(tr: pl.DataFrame) -> pl.DataFrame:
    """Ekipman farkına göre kazanma (pistol round'ları hariç; ikisi de eşit başlar)."""
    t = tr.filter(~pl.col("is_pistol") & pl.col("own_equip").is_not_null()
                  & pl.col("enemy_equip").is_not_null())
    order = {label: i for i, label in enumerate(EQUIP_LABELS)}
    # Sağdan kapalı aralıklar: (-∞, -10k], (-10k, -3k], (-3k, 3k], (3k, 10k], (10k, ∞)
    diff = pl.col("own_equip") - pl.col("enemy_equip")
    edge = pl.lit(EQUIP_LABELS[-1])
    for bound, label in reversed(list(zip(EQUIP_BINS, EQUIP_LABELS, strict=False))):
        edge = pl.when(diff <= bound).then(pl.lit(label)).otherwise(edge)
    return (
        t.with_columns(edge.alias("edge"))
        .group_by("edge")
        .agg(pl.len().alias("rounds"), pl.col("won").sum().alias("wins"))
        .with_columns(pl.col("edge").replace_strict(order, default=9).alias("_o"))
        .sort("_o").drop("_o")
    )
