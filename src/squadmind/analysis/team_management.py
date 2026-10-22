"""Takım yönetimi analizi (Faz 2c): kim nerede iyi, nereleri tutmalı, güç farkımız,
kime destek gerekli, kim kiminle uyumlu.

"Kadro" = oyuncu + maçların en az yarısında onunla aynı takımda oynayanlar (çekirdek);
geri kalan takım arkadaşları "diğer" olarak gruplanır. Tüm oranlar örneklemiyle verilir;
yorum `director` / `tactician` ajanlarının işi.
"""

from __future__ import annotations

import polars as pl

from squadmind.analysis.maps import LANES, ZONES
from squadmind.analysis.player_metrics import (
    CT_SETUP_WINDOW,
    DemoData,
    aggregate,
    death_isolation,
    player_match_metrics,
    player_rounds,
)
from squadmind.analysis.team_tactics import DEFAULT_WINDOW, TICK_RATE

_RK = ["match_id", "round_no"]
# Trade: takım arkadaşını öldüren rakip bu kadar saniye içinde öldürülürse
TRADE_SECONDS = 5.0
# İki oyuncu bu mesafeden yakınsa "birlikte oynuyor" (yalnız ölüm eşiğiyle aynı)
NEAR_DIST = 900.0
# Yakınlık ölçülen pencereler (sn): CT kurulumu ve T'nin map kontrolü + giriş
PAIR_WINDOWS = {"CT": CT_SETUP_WINDOW, "T": (15.0, 45.0)}
OTHER = "diğer"


def core_roster(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Oyuncu + maçların en az yarısında onunla oynayanlar: steam_id, nickname, matches."""
    mine = d.roster.filter(pl.col("steam_id") == steam_id).select("match_id", "faction")
    n = mine.height
    mates = (
        d.roster.join(mine, on=["match_id", "faction"])
        .group_by("steam_id").agg(pl.col("nickname").last(),
                                  pl.col("match_id").n_unique().alias("matches"))
    )
    return (mates.filter((pl.col("steam_id") == steam_id) | (pl.col("matches") * 2 >= n))
            .sort(["matches", "nickname"], descending=[True, False]))


def _team_player_rounds(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Takımın tüm oyuncularının round'ları; çekirdek dışı oyuncular 'diğer' adıyla."""
    mine = d.roster.filter(pl.col("steam_id") == steam_id).select("match_id", "faction")
    core = core_roster(d, steam_id).select("steam_id", pl.col("nickname").alias("_core"))
    names = d.roster.select("match_id", "steam_id", "nickname")
    return (
        player_rounds(d).join(mine, on=["match_id", "faction"])
        .join(names, on=["match_id", "steam_id"], how="left")
        .join(core, on="steam_id", how="left")
        .with_columns(pl.col("_core").fill_null(OTHER).alias("player"))
        .drop("_core")
        .cast({"round_no": pl.Int64})
    )


def _round_events(d: DemoData) -> pl.DataFrame:
    """(maç, round, oyuncu) başına kill, ölüm, opening kill / ölüm, trade edilen ölüm."""
    k = d.kills.cast({"round_no": pl.Int64})
    enemy = pl.col("attacker_side") != pl.col("victim_side")
    kills = (k.filter(enemy).group_by(*_RK, pl.col("attacker").alias("steam_id"))
             .agg(pl.len().alias("k"), pl.col("is_opening").sum().alias("ok")))
    deaths = (k.group_by(*_RK, pl.col("victim").alias("steam_id"))
              .agg(pl.len().alias("dth"), pl.col("is_opening").sum().alias("od"),
                   pl.col("is_traded").sum().alias("traded")))
    return kills.join(deaths, on=[*_RK, "steam_id"], how="full", coalesce=True)


def _place_rounds(d: DemoData, side: str, table: dict[str, dict[str, str]],
                  window: tuple[float, float], col: str) -> pl.DataFrame:
    """Her (maç, round, oyuncu) için penceredeki en sık alan / koridor (o taraf).

    Pencere başlamadan ölen oyuncu (erken opening ölümü) dışarıda kalmasın diye: pencerede
    kaydı olmayanlara round başından pencere sonuna kadarki SON kaydının alanı verilir.
    """
    rows = [(m, p, v) for m, places in table.items() for p, v in places.items()]
    pmap = pl.DataFrame(rows, schema=["map", "place_name", col], orient="row")
    pos = (
        d.positions.cast({"round_no": pl.Int64})
        .filter((pl.col("side") == side) & (pl.col("time_in_round") <= window[1]))
        .join(d.match_maps.select("match_id", "map"), on="match_id")
        .join(pmap, on=["map", "place_name"])
        .rename({"player": "steam_id"})
    )
    key = [*_RK, "steam_id"]
    in_window = (pos.filter(pl.col("time_in_round") >= window[0])
                 .group_by(key).agg(pl.col(col).mode().sort().first()))
    early = (pos.join(in_window, on=key, how="anti")
             .sort("tick").group_by(key).agg(pl.col(col).last()))
    return pl.concat([in_window, early])


def player_spots(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Kim nerede iyi: oyuncu × taraf × alan (CT: A / Mid / B) ya da koridor (T).

    CT alanı 15-35. sn'de en çok bulunulan; T koridoru 15-20. sn'deki (default kurulumu).
    O round'lardaki tüm kill / ölümler sayılır (sadece o bölgedekiler değil).
    Kolonlar: side, spot, player, rounds, wins, kills, deaths, ok (opening kill), od
    (opening ölüm), traded; lobi satırı player = "lobi" (aynı maçlardaki herkes).
    """
    tpr = _team_player_rounds(d, steam_id)
    ev = _round_events(d)
    spots = pl.concat([
        _place_rounds(d, "CT", ZONES, CT_SETUP_WINDOW, "spot").with_columns(
            pl.lit("CT").alias("side")),
        _place_rounds(d, "T", LANES, DEFAULT_WINDOW, "spot").with_columns(
            pl.lit("T").alias("side")),
    ])
    team_ids = tpr.select("match_id", "steam_id").unique()
    all_pr = (player_rounds(d).cast({"round_no": pl.Int64})
              .join(team_ids.select("match_id").unique(), on="match_id"))

    def summarise(rounds: pl.DataFrame, who: pl.Expr) -> pl.DataFrame:
        return (
            rounds.join(spots, on=[*_RK, "steam_id", "side"])
            .join(ev, on=[*_RK, "steam_id"], how="left")
            .group_by("side", "spot", who.alias("player"))
            .agg(pl.len().alias("rounds"), pl.col("won").sum().alias("wins"),
                 *(pl.col(c).fill_null(0).sum().alias(n) for c, n in (
                     ("k", "kills"), ("dth", "deaths"), ("ok", "ok"), ("od", "od"),
                     ("traded", "traded"))))
        )

    team = summarise(tpr, pl.col("player"))
    lobby = summarise(all_pr, pl.lit("lobi"))
    return pl.concat([team, lobby]).sort("side", "spot", "rounds", descending=[False, False, True])


def spot_ranking(spots: pl.DataFrame) -> pl.DataFrame:
    """Her alanda oyuncuları round başı (kill − ölüm) ile sıralar; eşitlikte round kazanma.

    Tek bir "en iyi" seçmez: aynı oyuncu birden çok alanda önde çıkabilir; atama
    (kim nereye) tactician'ın işi. Lobi ve 'diğer' satırları referans olarak kalır
    (rank boş). Eklenen kolonlar: net, win_rate, rank
    """
    core = ~pl.col("player").is_in(["lobi", OTHER])
    return (
        # Sayımlar işaretsiz tamsayı: çıkarmadan önce işaretliye çevir (negatif taşmasın)
        spots.with_columns(((pl.col("kills").cast(pl.Int64) - pl.col("deaths").cast(pl.Int64))
                            / pl.col("rounds")).alias("net"),
                           (pl.col("wins") / pl.col("rounds")).alias("win_rate"))
        .sort(["side", "spot", "net", "win_rate"], descending=[False, False, True, True])
        .with_columns(
            pl.when(core).then(pl.col("net").rank("ordinal", descending=True)
                               .over("side", "spot", core)).alias("rank"))
    )


def team_vs_opponents(d: DemoData, steam_id: str) -> tuple[dict, dict]:
    """Güç farkı: takımın ve aynı maçlardaki rakiplerin oranları (aggregate ile)."""
    pm = player_match_metrics(d)
    mine = d.roster.filter(pl.col("steam_id") == steam_id).select("match_id", "faction")
    team = pm.join(mine, on=["match_id", "faction"], how="semi")
    opp = pm.join(mine.select("match_id"), on="match_id", how="semi").join(
        mine, on=["match_id", "faction"], how="anti")
    return aggregate(team), aggregate(opp)


def support_needs(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Kime destek gerekli: oyuncu başına ölüm, yalnız ölüm, trade edilen, opening ölüm."""
    tpr = _team_player_rounds(d, steam_id).select(*_RK, "steam_id", "player", "side")
    iso = death_isolation(d).cast({"round_no": pl.Int64}).rename({"victim": "steam_id"})
    deaths = d.kills.cast({"round_no": pl.Int64}).rename({"victim": "steam_id"})
    per = (
        deaths.join(tpr, on=[*_RK, "steam_id"])
        .join(iso.select(*_RK, "tick", "steam_id", "isolated"),
              on=[*_RK, "tick", "steam_id"], how="left")
        .group_by("player", "side")
        .agg(pl.len().alias("deaths"),
             pl.col("isolated").is_not_null().sum().alias("measured"),
             pl.col("isolated").fill_null(False).sum().alias("isolated"),
             pl.col("is_traded").sum().alias("traded"),
             pl.col("is_opening").sum().alias("opening_deaths"))
    )
    rounds = tpr.group_by("player", "side").agg(pl.len().alias("rounds"))
    return per.join(rounds, on=["player", "side"]).sort("player", "side")


def trade_pairs(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Kim kimin ölümünü trade ediyor: Y'yi öldüren rakibi X, TRADE_SECONDS içinde öldürdü.

    Kolonlar: trader (X), traded (Y), n. Bir ölüm için ilk trade eden sayılır.
    """
    tpr = _team_player_rounds(d, steam_id).select(*_RK, "steam_id", "player")
    k = d.kills.cast({"round_no": pl.Int64, "tick": pl.Int64})
    window = int(TRADE_SECONDS * TICK_RATE)
    team_deaths = (k.filter(pl.col("attacker_side") != pl.col("victim_side"))
                   .join(tpr.rename({"steam_id": "victim", "player": "traded"}),
                         on=[*_RK, "victim"])
                   .select(*_RK, pl.col("tick").alias("dtick"), "traded",
                           pl.col("attacker").alias("enemy")))
    revenge = (k.join(tpr.rename({"steam_id": "attacker", "player": "trader"}),
                      on=[*_RK, "attacker"])
               .select(*_RK, pl.col("tick").alias("rtick"), "trader",
                       pl.col("victim").alias("enemy")))
    return (
        team_deaths.join(revenge, on=[*_RK, "enemy"])
        .filter((pl.col("rtick") > pl.col("dtick")) & (pl.col("rtick") - pl.col("dtick") <= window))
        .sort("rtick").group_by(*_RK, "dtick", "traded", maintain_order=True).first()
        .group_by("trader", "traded").agg(pl.len().alias("n"))
        .sort("n", descending=True)
    )


def proximity_pairs(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Kim kiminle yakın oynuyor: aynı anda hayattayken ≤ NEAR_DIST olunan kayıt payı.

    Kolonlar: side, a, b (çekirdek oyuncular, a < b), samples, near (yakın kayıt)
    """
    core = core_roster(d, steam_id).select("steam_id", pl.col("nickname").alias("name"))
    pos = (d.positions.cast({"round_no": pl.Int64})
           .join(core.rename({"steam_id": "player"}), on="player"))
    parts = []
    for side, window in PAIR_WINDOWS.items():
        p = pos.filter((pl.col("side") == side) & pl.col("time_in_round").is_between(*window))
        has_z = "z" in p.columns
        a = p.select(*_RK, "tick", pl.col("name").alias("a"), pl.col("x").alias("ax"),
                     pl.col("y").alias("ay"), *([pl.col("z").alias("az")] if has_z else []))
        b = p.select(*_RK, "tick", pl.col("name").alias("b"), pl.col("x").alias("bx"),
                     pl.col("y").alias("by"), *([pl.col("z").alias("bz")] if has_z else []))
        dist2 = (pl.col("ax") - pl.col("bx")) ** 2 + (pl.col("ay") - pl.col("by")) ** 2
        if has_z:
            dist2 = dist2 + (pl.col("az") - pl.col("bz")) ** 2
        parts.append(
            a.join(b, on=[*_RK, "tick"]).filter(pl.col("a") < pl.col("b"))
            .group_by("a", "b").agg(pl.len().alias("samples"),
                                    (dist2.sqrt() <= NEAR_DIST).sum().alias("near"))
            .with_columns(pl.lit(side).alias("side"))
        )
    return pl.concat(parts).select("side", "a", "b", "samples", "near").sort("side", "a", "b")
