"""Hasarın kurbanın kalan canına göre kırpılması (ADR mantığı).

Demodaki `dmg_health` ham değerdir: 20 canı kalan rakibe AWP isabeti 115 yazar.
ADR ve hasar karşılaştırmaları için kırpılmış değer gerekir. Bu modül awpy'ye
bağlı değildir; hem parser hem analiz katmanı kullanır.
"""

from __future__ import annotations

import polars as pl

# Round başı can (CS2'de her round 100)
START_HEALTH = 100


def cap_damage(df: pl.DataFrame, keys: list[str], raw: str = "dmg_health",
               out: str = "dmg") -> pl.DataFrame:
    """`out` kolonu ekler: her hasar olayının kalan cana göre kırpılmış değeri.

    `keys` bir kurbanın bir round'unu tanımlar (ör. ["match_id", "round_no", "victim"]).
    Round içinde can yenilenmediği için kurbanın kırpılmış toplamı
    min(100, ham kümülatif toplam) olur; her olayın payı bu toplamdaki artıştır.
    Takım arkadaşından gelen hasar da canı düşürdüğü için hesaba girer.
    18 Dust2 maçında 180/180 oyuncu-maç ADR'ı FACEIT ile en fazla 2,3 farkla tuttu.
    """
    cum = pl.min_horizontal(pl.lit(START_HEALTH), pl.col(raw).fill_null(0).cum_sum().over(keys))
    return (
        df.sort("tick")
        .with_columns(cum.alias("_cum"))
        .with_columns(
            (pl.col("_cum") - pl.col("_cum").shift(1, fill_value=0).over(keys))
            .cast(pl.Int32).alias(out)
        )
        .drop("_cum")
    )
