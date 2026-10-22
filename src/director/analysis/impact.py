"""Kill ve hasarın takıma kattığı değer.

Aynı sayıdaki iki kill aynı değerde değildir:
- **Kill türü:** kurbanın kaybettiği 100 canın ne kadarını katil verdi? Tam canlı
  rakibi tek başına indirmek (solo) ile takım arkadaşının yaraladığını bitirmek farklıdır.
- **Durum:** 5v5'te opening ile 5v1'de alınan kill farklıdır. Sağ kalan oyuncu
  sayısına göre round kazanma olasılığı bu maçların kendi verisinden çıkarılır; kill'in
  değeri bu olasılığa eklediği puandır (WPA, "win probability added").
- **Ekonomi:** eco'daki rakibe kill ucuzdur; kendi takımın eco / force'tayken full buy
  rakibe kill pahalıdır. Kill ile rakibe kaybettirilen ekipman değeri de ölçülür.
- **Hasar:** öldürmeyen hasar da iş görür. Low bırakılan rakibi takım arkadaşı bitirirse
  kill ona yazılır; burada o hasar "hasar desteği" ve asist olarak görünür olur.

Hasar daima kalan cana göre kırpılmış `dmg` kolonudur (bkz. `director.parse.damage`).
"""

from __future__ import annotations

import polars as pl

from director.analysis.player_metrics import DemoData, Rate, _weapon_category, player_rounds

# Takımdaki oyuncu sayısı (sağ kalan sayısı bundan düşülerek bulunur)
TEAM_SIZE = 5
# Kill türü: katilin kurbana verdiği (kırpılmış) hasar payı
SOLO_DMG = 90       # ≥ 90: tam canlı rakibi tek başına indirdi
FINISH_DMG = 40     # < 40: yaralı rakibi bitirdi; arası "ortak"
# Low bırakma: hasardan sonra kurbanın kalan canı bu değer ve altı
LOW_HP = 40
# CS2'nin asist eşiği: takım arkadaşının öldürdüğü rakibe en az bu kadar hasar
ASSIST_DMG = 41
# "Rahat durumda kill": kill öncesi sayı üstünlüğü en az bu kadar (ör. 4v2)
COMFORT_LEAD = 2
# Kazanma olasılığı tablosunda yumuşatma ağırlığı (az görülen durumlar için)
WP_PRIOR = 10.0

_RK = ["match_id", "round_no"]
_EMPTY_DMG = {"match_id": pl.String, "round_no": pl.Int64, "tick": pl.Int64,
              "attacker": pl.String, "attacker_side": pl.String, "victim": pl.String,
              "victim_side": pl.String, "weapon": pl.String, "dmg": pl.Int32}


def _damages(d: DemoData) -> pl.DataFrame:
    """Kırpılmış hasar tablosu; hasar verisi olmayan (eski parser) durumda boş."""
    if d.damages is None or d.damages.height == 0 or "dmg" not in d.damages.columns:
        return pl.DataFrame(schema=_EMPTY_DMG)
    return d.damages.cast({"round_no": pl.Int64, "tick": pl.Int64})


def _side_buys(d: DemoData) -> pl.DataFrame:
    """Her round için iki tarafın alım tipi, uzun formatta: (maç, round, side, buy)."""
    r = d.rounds.cast({"round_no": pl.Int64})
    return pl.concat([
        r.select(*_RK, pl.lit("CT").alias("side"), pl.col("ct_buy_type").alias("buy")),
        r.select(*_RK, pl.lit("T").alias("side"), pl.col("t_buy_type").alias("buy")),
    ])


# --- sağ kalan sayısı ve kazanma olasılığı ------------------------------------

def _kills_with_state(d: DemoData) -> pl.DataFrame:
    """Tüm ölümler (takım içi, dünya dahil) + her ölümden ÖNCE iki tarafın ölü sayısı."""
    k = d.kills.cast({"round_no": pl.Int64, "tick": pl.Int64}).sort(*_RK, "tick")
    ct = (pl.col("victim_side") == "CT").cast(pl.Int32)
    t = (pl.col("victim_side") == "T").cast(pl.Int32)
    return k.with_columns(
        (ct.cum_sum().over(_RK) - ct).alias("ct_dead_before"),
        (t.cum_sum().over(_RK) - t).alias("t_dead_before"),
    )


def win_prob_table(d: DemoData) -> pl.DataFrame:
    """P(round kazanma | taraf, kendi sağ kalan, rakip sağ kalan), bu maçların verisinden.

    Her round her taraf için 5v5 başlangıcı ve her ölümden sonraki durum bir gözlemdir.
    Az görülen durum, iki tarafın birleşik oranına doğru çekilir (WP_PRIOR ağırlığıyla).
    Ekonomi hesaba katılmaz; o ayrıca ölçülür.
    """
    k = _kills_with_state(d).with_columns(
        (pl.col("ct_dead_before") + (pl.col("victim_side") == "CT")).alias("ct_dead"),
        (pl.col("t_dead_before") + (pl.col("victim_side") == "T")).alias("t_dead"),
    ).select(*_RK, "ct_dead", "t_dead")
    starts = d.rounds.select(*_RK).cast({"round_no": pl.Int64}).with_columns(
        pl.lit(0).alias("ct_dead"), pl.lit(0).alias("t_dead"))
    states = pl.concat([starts, k.cast({"ct_dead": pl.Int32, "t_dead": pl.Int32})],
                       how="vertical_relaxed")
    winners = d.rounds.select(*_RK, "winner_side").cast({"round_no": pl.Int64})
    obs = states.join(winners, on=_RK)
    long = pl.concat([
        obs.select((TEAM_SIZE - pl.col("ct_dead")).alias("own"),
                   (TEAM_SIZE - pl.col("t_dead")).alias("enemy"),
                   pl.lit("CT").alias("side"), (pl.col("winner_side") == "CT").alias("won")),
        obs.select((TEAM_SIZE - pl.col("t_dead")).alias("own"),
                   (TEAM_SIZE - pl.col("ct_dead")).alias("enemy"),
                   pl.lit("T").alias("side"), (pl.col("winner_side") == "T").alias("won")),
    ]).with_columns(pl.col("own").clip(0, TEAM_SIZE), pl.col("enemy").clip(0, TEAM_SIZE))
    by_side = long.group_by("side", "own", "enemy").agg(
        pl.len().alias("n"), pl.col("won").sum().alias("w"))
    pooled = long.group_by("own", "enemy").agg(
        pl.len().alias("n_all"), pl.col("won").sum().alias("w_all"))
    return (
        by_side.join(pooled, on=["own", "enemy"])
        .with_columns(((pl.col("w_all") + 1) / (pl.col("n_all") + 2)).alias("_pp"))
        .with_columns(((pl.col("w") + WP_PRIOR * pl.col("_pp")) / (pl.col("n") + WP_PRIOR))
                      .alias("p"))
        .select("side", "own", "enemy", "n", "p")
    )


def _lookup_p(df: pl.DataFrame, wp: pl.DataFrame, own: str, enemy: str, out: str) -> pl.DataFrame:
    """(side, own, enemy) için kazanma olasılığı; tabloda olmayan durum 0,5."""
    look = wp.select("side", pl.col("own").alias(own), pl.col("enemy").alias(enemy),
                     pl.col("p").alias(out))
    return (df.join(look, left_on=["attacker_side", own, enemy],
                    right_on=["side", own, enemy], how="left")
            .with_columns(pl.col(out).fill_null(0.5)))


# --- kill tablosu --------------------------------------------------------------

def kill_table(d: DemoData) -> pl.DataFrame:
    """Rakibe atılan her kill, değer bağlamıyla.

    Kolonlar (ham kill kolonlarına ek): killer_dmg (katilin kurbana bu round verdiği
    kırpılmış hasar), kill_type (solo / ortak / bitirme), own_alive / enemy_alive (kill
    öncesi), wpa, own_buy / enemy_buy, victim_equip / killer_equip (son konum kaydındaki
    ekipman değeri), wcat (silah kategorisi)
    """
    k = _kills_with_state(d).filter(
        pl.col("attacker").is_not_null() & (pl.col("attacker_side") != pl.col("victim_side"))
    ).with_columns(
        pl.when(pl.col("attacker_side") == "CT")
        .then(TEAM_SIZE - pl.col("ct_dead_before")).otherwise(TEAM_SIZE - pl.col("t_dead_before"))
        .clip(1, TEAM_SIZE).alias("own_alive"),
        pl.when(pl.col("victim_side") == "CT")
        .then(TEAM_SIZE - pl.col("ct_dead_before")).otherwise(TEAM_SIZE - pl.col("t_dead_before"))
        .clip(1, TEAM_SIZE).alias("enemy_alive"),
    ).with_columns((pl.col("enemy_alive") - 1).alias("enemy_after"))

    # Katilin kurbana bu round kill anına kadar verdiği hasar
    dm = _damages(d)
    kd = (
        k.select(*_RK, "tick", "attacker", "victim")
        .join(dm.select(*_RK, pl.col("tick").alias("dtick"), "attacker", "victim", "dmg"),
              on=[*_RK, "attacker", "victim"])
        .filter(pl.col("dtick") <= pl.col("tick"))
        .group_by(*_RK, "tick", "attacker", "victim").agg(pl.col("dmg").sum().alias("killer_dmg"))
    )
    k = k.join(kd, on=[*_RK, "tick", "attacker", "victim"], how="left").with_columns(
        pl.col("killer_dmg").fill_null(0))
    has_dmg = dm.height > 0
    k = k.with_columns(
        pl.when(pl.lit(not has_dmg)).then(pl.lit(None, pl.String))
        .when(pl.col("killer_dmg") >= SOLO_DMG).then(pl.lit("solo"))
        .when(pl.col("killer_dmg") < FINISH_DMG).then(pl.lit("bitirme"))
        .otherwise(pl.lit("ortak")).alias("kill_type"),
        _weapon_category().alias("wcat"),
    )

    # Kazanma olasılığı katkısı
    wp = win_prob_table(d)
    k = _lookup_p(k, wp, "own_alive", "enemy_alive", "p_before")
    k = _lookup_p(k, wp, "own_alive", "enemy_after", "p_after")
    k = k.with_columns((pl.col("p_after") - pl.col("p_before")).alias("wpa"))

    # Alım tipleri
    buys = _side_buys(d)
    k = (k.join(buys.rename({"side": "attacker_side", "buy": "own_buy"}),
                on=[*_RK, "attacker_side"], how="left")
         .join(buys.rename({"side": "victim_side", "buy": "enemy_buy"}),
               on=[*_RK, "victim_side"], how="left"))

    # Ekipman değeri: kill anından önceki son konum kaydı (2 sn aralık; yaklaşık)
    if "equipment_value" in d.positions.columns:
        pos = (d.positions.select(*_RK, pl.col("tick").cast(pl.Int64), "player",
                                  "equipment_value")
               .cast({"round_no": pl.Int64}).sort("tick"))
        for who, out in (("victim", "victim_equip"), ("attacker", "killer_equip")):
            k = k.sort("tick").join_asof(
                pos.rename({"player": who, "equipment_value": out}),
                on="tick", by=[*_RK, who], strategy="backward",
                check_sortedness=False,  # iki taraf da tick'e göre sıralı
            )
    else:
        k = k.with_columns(pl.lit(None, pl.Int64).alias("victim_equip"),
                           pl.lit(None, pl.Int64).alias("killer_equip"))
    return k


# --- hasar sonuçları -------------------------------------------------------------

def damage_outcomes(d: DemoData) -> pl.DataFrame:
    """Her (round, saldıran, rakip kurban) çifti: verilen hasar ve kurbana ne oldu.

    outcome: "ben" (saldıran öldürdü) / "takım" (takım arkadaşı öldürdü) /
    "hayatta" (round'u sağ bitirdi) / "diğer" (dünya, takım içi vb.)
    hp_after: saldıranın son isabetinden sonra kurbanın kalan canı (tüm kaynaklar dahil)
    """
    dm = _damages(d)
    if dm.height == 0:
        return pl.DataFrame(schema={**{c: pl.String for c in ("match_id", "attacker",
                                       "attacker_side", "victim", "weapon", "outcome")},
                                    "round_no": pl.Int64, "my_dmg": pl.Int64,
                                    "hp_after": pl.Int64})
    # Kurbanın her isabetten sonraki canı (kırpılmış kümülatif; tüm saldıranlar)
    dm = dm.sort("tick").with_columns(
        (100 - pl.col("dmg").cum_sum().over([*_RK, "victim"])).alias("hp_after"))
    pairs = (
        dm.filter(pl.col("attacker").is_not_null()
                  & (pl.col("attacker_side") != pl.col("victim_side")))
        .group_by(*_RK, "attacker", "victim")
        .agg(pl.col("dmg").sum().cast(pl.Int64).alias("my_dmg"),
             pl.col("attacker_side").first(),
             pl.col("hp_after").last().cast(pl.Int64),
             pl.col("weapon").last())
        .filter(pl.col("my_dmg") > 0)
    )
    deaths = (
        d.kills.cast({"round_no": pl.Int64}).sort("tick")
        .group_by(*_RK, "victim")
        .agg(pl.col("attacker").first().alias("killer"),
             pl.col("attacker_side").first().alias("killer_side"),
             pl.lit(True).alias("died"))
    )
    return pairs.join(deaths, on=[*_RK, "victim"], how="left").with_columns(
        pl.when(pl.col("died").is_null()).then(pl.lit("hayatta"))
        .when(pl.col("killer") == pl.col("attacker")).then(pl.lit("ben"))
        .when(pl.col("killer_side") == pl.col("attacker_side")).then(pl.lit("takım"))
        .otherwise(pl.lit("diğer")).alias("outcome")
    ).drop("killer", "killer_side", "died")


# --- (maç, oyuncu) sayımları ve oranlar ---------------------------------------------

_COUNT_COLS = [
    "kills", "solo_kills", "shared_kills", "finish_kills", "typed_kills",
    "wpa", "behind_kills", "comfort_kills", "eco_kills", "disadv_kills", "equip_destroyed",
    "equip_kills", "rounds", "disadv_rounds", "vs_eco_rounds",
    "dmg_total", "dmg_to_dead", "support_dmg", "assists", "low_left", "low_converted",
]


def impact_counts(d: DemoData) -> pl.DataFrame:
    """Her (maç, oyuncu) için kill / hasar değeri ham sayımları. Oranlar `impact_rates`."""
    k = kill_table(d)
    eco = pl.col("enemy_buy") == "eco"
    poor = pl.col("own_buy").is_in(["eco", "force"]) & (pl.col("enemy_buy") == "full")
    kc = k.group_by("match_id", pl.col("attacker").alias("steam_id")).agg(
        pl.len().alias("kills"),
        (pl.col("kill_type") == "solo").sum().alias("solo_kills"),
        (pl.col("kill_type") == "ortak").sum().alias("shared_kills"),
        (pl.col("kill_type") == "bitirme").sum().alias("finish_kills"),
        pl.col("kill_type").is_not_null().sum().alias("typed_kills"),
        pl.col("wpa").sum().alias("wpa"),
        (pl.col("own_alive") < pl.col("enemy_alive")).sum().alias("behind_kills"),
        (pl.col("own_alive") - pl.col("enemy_alive") >= COMFORT_LEAD).sum().alias("comfort_kills"),
        eco.sum().alias("eco_kills"),
        poor.sum().alias("disadv_kills"),
        pl.col("victim_equip").sum().alias("equip_destroyed"),
        pl.col("victim_equip").is_not_null().sum().alias("equip_kills"),
    )

    # Round sayıları: toplam, dezavantajlı ekonomi, eco'daki rakibe karşı
    buys = _side_buys(d)
    pr = (player_rounds(d).cast({"round_no": pl.Int64})
          .with_columns(pl.when(pl.col("side") == "CT").then(pl.lit("T"))
                        .otherwise(pl.lit("CT")).alias("enemy_side"))
          .join(buys.rename({"side": "enemy_side", "buy": "enemy_buy"}),
                on=[*_RK, "enemy_side"], how="left"))
    rc = pr.group_by("match_id", "steam_id").agg(
        pl.len().alias("rounds"),
        (pl.col("own_buy").is_in(["eco", "force"]) & (pl.col("enemy_buy") == "full"))
        .sum().alias("disadv_rounds"),
        ((pl.col("enemy_buy") == "eco") & (pl.col("own_buy") != "pistol"))
        .sum().alias("vs_eco_rounds"),
    )

    o = damage_outcomes(d)
    low = (pl.col("hp_after") <= LOW_HP) & (pl.col("outcome") != "ben")
    dc = o.group_by("match_id", pl.col("attacker").alias("steam_id")).agg(
        pl.col("my_dmg").sum().alias("dmg_total"),
        pl.col("my_dmg").filter(pl.col("outcome").is_in(["ben", "takım", "diğer"]))
        .sum().alias("dmg_to_dead"),
        pl.col("my_dmg").filter(pl.col("outcome") == "takım").sum().alias("support_dmg"),
        ((pl.col("outcome") == "takım") & (pl.col("my_dmg") >= ASSIST_DMG)).sum().alias("assists"),
        low.sum().alias("low_left"),
        (low & (pl.col("outcome") == "takım")).sum().alias("low_converted"),
    )
    out = rc.join(kc, on=["match_id", "steam_id"], how="left").join(
        dc, on=["match_id", "steam_id"], how="left")
    return out.with_columns(pl.col(c).fill_null(0) for c in _COUNT_COLS)


def impact_rates(df: pl.DataFrame) -> dict[str, Rate]:
    """`impact_counts` satırlarını (bir oyuncu ya da lobi) oranlara çevirir."""
    s = {c: float(df[c].sum()) if df.height else 0.0 for c in _COUNT_COLS}
    return {
        "solo_kill_share": Rate(s["solo_kills"], s["typed_kills"]),
        "finish_kill_share": Rate(s["finish_kills"], s["typed_kills"]),
        "wpa_per_round": Rate(s["wpa"], s["rounds"]),
        "wpa_per_kill": Rate(s["wpa"], s["kills"]),
        "behind_kill_share": Rate(s["behind_kills"], s["kills"]),
        "comfort_kill_share": Rate(s["comfort_kills"], s["kills"]),
        "eco_kill_share": Rate(s["eco_kills"], s["kills"]),
        "disadv_kpr": Rate(s["disadv_kills"], s["disadv_rounds"]),
        "equip_per_kill": Rate(s["equip_destroyed"], s["equip_kills"]),
        "assists_per_round": Rate(s["assists"], s["rounds"]),
        "support_dmg_per_round": Rate(s["support_dmg"], s["rounds"]),
        "low_left_per_round": Rate(s["low_left"], s["rounds"]),
        "low_converted": Rate(s["low_converted"], s["low_left"]),
        "dmg_conversion": Rate(s["dmg_to_dead"], s["dmg_total"]),
    }


def kill_value_by_weapon(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Oyuncunun silah kategorisine göre kill değeri ve öldürmeyen hasarı.

    Kill kolonları kill'i atan silaha, hasar kolonları (low bırakma, hasar desteği)
    kurbana son isabeti yapan silaha göredir.
    """
    k = kill_table(d).filter(pl.col("attacker") == steam_id)
    kv = k.group_by("wcat").agg(
        pl.len().alias("kills"),
        (pl.col("kill_type") == "solo").sum().alias("solo"),
        (pl.col("kill_type") == "bitirme").sum().alias("finish"),
        pl.col("wpa").sum().alias("wpa"),
        (pl.col("enemy_buy") == "eco").sum().alias("eco_kills"),
        pl.col("victim_equip").mean().alias("equip_per_kill"),
    )
    o = damage_outcomes(d).filter(pl.col("attacker") == steam_id).with_columns(
        _weapon_category().alias("wcat"))
    dv = o.group_by("wcat").agg(
        ((pl.col("hp_after") <= LOW_HP) & (pl.col("outcome") != "ben")).sum().alias("low_left"),
        ((pl.col("hp_after") <= LOW_HP) & (pl.col("outcome") == "takım")).sum()
        .alias("low_converted"),
        pl.col("my_dmg").filter(pl.col("outcome") == "takım").sum().alias("support_dmg"),
        ((pl.col("outcome") == "takım") & (pl.col("my_dmg") >= ASSIST_DMG)).sum().alias("assists"),
    )
    return (kv.join(dv, on="wcat", how="full", coalesce=True)
            .with_columns(pl.col(c).fill_null(0) for c in
                          ("kills", "solo", "finish", "wpa", "eco_kills", "low_left",
                           "low_converted", "support_dmg", "assists"))
            .rename({"wcat": "weapon"})
            .sort("kills", descending=True))
