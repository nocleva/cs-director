"""Takım taktik desenleri (CLAUDE.md bölüm 6, Faz 2a).

"Takım" = oyuncunun her maçtaki takımı (FACEIT'te kadro maçtan maça değişebilir).

T tarafı, her round için kural tabanlı:
1. Hedef site: plant edildiyse o site; değilse T'lerin giriş alanında (HIT_AREAS) en çok
   bulunduğu site. Hiç girilmediyse desen "girilmedi" (save, erken ölüm).
2. Giriş anı: en az COMMIT_PLAYERS T'nin (hayatta olan daha azsa hepsinin, en az 2)
   aynı anda hedef sitenin giriş alanında olduğu ilk konum kaydı.
3. Desen: giriş ≤ RUSH_SECONDS → "rush"; değilse giren oyuncular ≥2 farklı koridordan
   geldiyse "split", tek koridordan "execute".
4. Default: 15-20. saniyede hayattaki T'ler ≥ DEFAULT_LANES koridora yayılmışsa round
   default'tan başlamış sayılır (desenden bağımsız bir işaret).

CT tarafı: 15-35. saniyede her CT'nin en çok bulunduğu alan (A / Mid / B) → "A-Mid-B"
dizilişi (ör. "2-1-2"); plant edilen round'larda site bazında CT kazanma (retake).
"""

from __future__ import annotations

import polars as pl

from squadmind.analysis.maps import HIT_AREAS, LANES, ZONES
from squadmind.analysis.player_metrics import CT_SETUP_WINDOW, DemoData, player_rounds

# Demo tick hızı (FACEIT CS2 demoları 64 tick); plant zamanını saniyeye çevirmek için
TICK_RATE = 64.0
# Site'a giriş: aynı anda giriş alanında en az bu kadar T
COMMIT_PLAYERS = 3
# Bu saniyeye kadar girilen round "rush" (plant'ların en hızlı %10'u B'de ~28, A'da ~34 sn)
RUSH_SECONDS = 25.0
# Default tespiti: bu penceredeki koridor dağılımı
DEFAULT_WINDOW = (15.0, 20.0)
DEFAULT_LANES = 3
# Split: giriş anı etrafında bu kadar saniye içinde siteye girenlerin koridorlarına bakılır
SPLIT_WINDOW = 10.0
# Bu kadar round'un altındaki satırlar raporda "az örnek" diye işaretlenir
MIN_ROUNDS = 20

_RK = ["match_id", "round_no"]


def _place_map(table: dict[str, dict[str, str]], name: str) -> pl.DataFrame:
    rows = [(m, p, v) for m, places in table.items() for p, v in places.items()]
    return pl.DataFrame(rows, schema=["map", "place_name", name], orient="row")


def team_rounds(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Oyuncunun takımının her round'u: taraf, sonuç, iki tarafın alımı, plant bilgisi.

    Kolonlar: match_id, round_no, map, faction, side, won, own_buy, enemy_buy,
    own_equip, enemy_equip, bomb_site, plant_time (sn), reason, is_pistol
    """
    pr = (player_rounds(d).filter(pl.col("steam_id") == steam_id)
          .select(*_RK, "faction", "side", "won", "is_pistol").cast({"round_no": pl.Int64}))
    r = d.rounds.cast({"round_no": pl.Int64}).select(
        *_RK, "ct_buy_type", "t_buy_type", "ct_equipment", "t_equipment", "bomb_site",
        "reason",
        ((pl.col("plant_tick") - pl.col("freeze_end_tick")) / TICK_RATE).alias("plant_time"),
    )
    ct = pl.col("side") == "CT"
    return (
        pr.join(r, on=_RK)
        .join(d.match_maps.select("match_id", "map"), on="match_id")
        .with_columns(
            pl.when(ct).then("ct_buy_type").otherwise("t_buy_type").alias("own_buy"),
            pl.when(ct).then("t_buy_type").otherwise("ct_buy_type").alias("enemy_buy"),
            pl.when(ct).then("ct_equipment").otherwise("t_equipment").alias("own_equip"),
            pl.when(ct).then("t_equipment").otherwise("ct_equipment").alias("enemy_equip"),
        )
        .drop("ct_buy_type", "t_buy_type", "ct_equipment", "t_equipment")
        .sort(*_RK)
    )


def _team_positions(d: DemoData, tr: pl.DataFrame, side: str) -> pl.DataFrame:
    """Takımın o taraftaki round'larında takım oyuncularının konum kayıtları."""
    rounds = tr.filter(pl.col("side") == side).select(*_RK, "map")
    return (
        d.positions.cast({"round_no": pl.Int64})
        .filter(pl.col("side") == side)
        .join(rounds, on=_RK)
    )


# --- T tarafı ---------------------------------------------------------------------

def t_round_patterns(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Takımın her T round'u için desen.

    Kolonlar: team_rounds kolonları + target_site, commit_time, pattern
    (rush / execute / split / girilmedi), lanes (giren oyuncuların koridorları),
    default (bool; koridor bilgisi olmayan haritada boş)
    """
    tr = team_rounds(d, steam_id)
    t_rounds = tr.filter(pl.col("side") == "T")
    pos = (_team_positions(d, tr, "T")
           .join(_place_map(LANES, "lane"), on=["map", "place_name"], how="left")
           .join(_place_map(HIT_AREAS, "hit_site"), on=["map", "place_name"], how="left"))

    # Default: 15-20. sn'de her oyuncunun en sık koridoru → round'da kaç farklı koridor
    setup = (
        pos.filter(pl.col("time_in_round").is_between(*DEFAULT_WINDOW)
                   & pl.col("lane").is_not_null())
        .group_by(*_RK, "player").agg(pl.col("lane").mode().sort().first())
        .group_by(_RK).agg(pl.col("lane").n_unique().alias("setup_lanes"))
    )

    # Hedef site: plant edilen; yoksa giriş alanında en çok kayıt olan site
    seen = (pos.filter(pl.col("hit_site").is_not_null())
            .group_by(*_RK, "hit_site").agg(pl.len().alias("_n"))
            .sort(["_n", "hit_site"], descending=[True, False])
            .group_by(_RK, maintain_order=True).first()
            .select(*_RK, pl.col("hit_site").alias("_seen_site")))
    target = (t_rounds.select(*_RK, "bomb_site").join(seen, on=_RK, how="left")
              .select(*_RK, pl.coalesce("bomb_site", "_seen_site").alias("target_site")))

    # Giriş anı: hedef sitenin giriş alanında aynı anda yeterli T
    alive = pos.group_by(*_RK, "tick").agg(pl.len().alias("_alive"))
    in_site = pos.join(target, on=_RK).filter(pl.col("hit_site") == pl.col("target_site"))
    commit = (
        in_site.group_by(*_RK, "tick").agg(pl.len().alias("_in"),
                                           pl.col("time_in_round").min().alias("_t"))
        .join(alive, on=[*_RK, "tick"])
        .filter(pl.col("_in") >= pl.max_horizontal(
            pl.lit(2), pl.min_horizontal(pl.lit(COMMIT_PLAYERS), pl.col("_alive"))))
        .group_by(_RK).agg(pl.col("_t").min().alias("commit_time"))
    )

    # Giren her oyuncunun koridoru: siteye ilk girişinden önceki son koridor
    first_in = in_site.group_by(*_RK, "player").agg(pl.col("tick").min().alias("_entry"),
                                                   pl.col("time_in_round").min().alias("_et"))
    approach = (
        pos.filter(pl.col("lane").is_not_null())
        .join(first_in, on=[*_RK, "player"])
        .filter(pl.col("tick") < pl.col("_entry"))
        .sort("tick").group_by(*_RK, "player").last()
        .select(*_RK, "player", "lane", "_et")
    )
    lanes = (
        approach.join(commit, on=_RK)
        .filter((pl.col("_et") - pl.col("commit_time")).abs() <= SPLIT_WINDOW)
        .group_by(_RK).agg(pl.col("lane").unique().sort().alias("lanes"))
    )

    out = (t_rounds.join(target, on=_RK, how="left").join(commit, on=_RK, how="left")
           .join(lanes, on=_RK, how="left").join(setup, on=_RK, how="left")
           # Plant edildi ama hiçbir kayıtta yeterli T birlikte görünmedi (ör. tek
           # oyuncunun geç plant'ı): giriş anı plant anı sayılır
           .with_columns(pl.coalesce("commit_time", "plant_time").alias("commit_time")))
    has_lanes = pl.col("map").is_in(list(LANES))
    return out.with_columns(
        pl.when(pl.col("commit_time").is_null()).then(pl.lit("girilmedi"))
        .when(pl.col("commit_time") <= RUSH_SECONDS).then(pl.lit("rush"))
        .when(pl.col("lanes").list.len() >= 2).then(pl.lit("split"))
        .otherwise(pl.lit("execute")).alias("pattern"),
        pl.when(has_lanes).then(pl.col("setup_lanes").fill_null(0) >= DEFAULT_LANES)
        .alias("default"),
    ).drop("setup_lanes")


def pattern_summary(tp: pl.DataFrame) -> pl.DataFrame:
    """Desen × site: round, kazanma, plant, plant sonrası kazanma, default'tan çıkan."""
    planted = pl.col("bomb_site").is_not_null()
    return (
        tp.group_by("pattern", "target_site")
        .agg(
            pl.len().alias("rounds"),
            pl.col("won").sum().alias("wins"),
            planted.sum().alias("plants"),
            (planted & pl.col("won")).sum().alias("plant_wins"),
            pl.col("default").fill_null(False).sum().alias("from_default"),
            pl.col("commit_time").median().alias("commit_median"),
        )
        .sort("rounds", descending=True)
    )


def lane_summary(tp: pl.DataFrame) -> pl.DataFrame:
    """Split / execute'ta kullanılan koridor kombinasyonları ve sonuçları."""
    return (
        tp.filter(pl.col("pattern").is_in(["split", "execute", "rush"])
                  & pl.col("lanes").is_not_null())
        .with_columns(pl.col("lanes").list.join(" + ").alias("route"))
        .group_by("pattern", "target_site", "route")
        .agg(pl.len().alias("rounds"), pl.col("won").sum().alias("wins"))
        .sort("rounds", descending=True)
    )


# --- CT tarafı --------------------------------------------------------------------

def ct_setups(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Takımın her CT round'u için diziliş: "A-Mid-B" sayıları (ör. "2-1-2").

    Pencerede (15-35. sn) konum kaydı olan her CT, en çok bulunduğu alana sayılır;
    sadece 5 CT'nin de yerinin bilindiği round'lar dizilişe girer (erken ölüm dışarıda).
    """
    tr = team_rounds(d, steam_id)
    pos = (_team_positions(d, tr, "CT")
           .filter(pl.col("time_in_round").is_between(*CT_SETUP_WINDOW))
           .join(_place_map(ZONES, "zone"), on=["map", "place_name"], how="inner"))
    per_player = (pos.group_by(*_RK, "player")
                  .agg(pl.col("zone").mode().sort().first()))
    counts = per_player.group_by(_RK).agg(
        (pl.col("zone") == "A").sum().alias("a"),
        (pl.col("zone") == "Mid").sum().alias("mid"),
        (pl.col("zone") == "B").sum().alias("b"),
        pl.len().alias("known"),
    )
    return (
        tr.filter(pl.col("side") == "CT").join(counts, on=_RK, how="left")
        .with_columns(
            pl.when(pl.col("known") == 5)
            .then(pl.format("{}-{}-{}", "a", "mid", "b")).alias("setup"))
    )


def setup_summary(cs: pl.DataFrame) -> pl.DataFrame:
    """CT dizilişlerinin sıklığı ve kazanma oranı (bilinmeyen diziliş hariç)."""
    return (
        cs.filter(pl.col("setup").is_not_null())
        .group_by("setup")
        .agg(pl.len().alias("rounds"), pl.col("won").sum().alias("wins"))
        .sort("rounds", descending=True)
    )


def site_summary(tr: pl.DataFrame) -> pl.DataFrame:
    """Site bazında plant ve plant sonrası kazanma; T (bizim plant) ve CT (retake)."""
    planted = tr.filter(pl.col("bomb_site").is_not_null())
    return (
        planted.group_by("side", "bomb_site")
        .agg(pl.len().alias("plants"), pl.col("won").sum().alias("wins"),
             pl.col("plant_time").median().alias("plant_median"))
        .join(tr.group_by("side").agg(pl.len().alias("side_rounds")), on="side")
        .sort("side", "bomb_site")
    )
