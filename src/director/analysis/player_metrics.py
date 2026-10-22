"""Oyuncu metrikleri (CLAUDE.md bölüm 6) — demo tablolarından.

Yaklaşım:
1. `load_demo_data`: işlenmiş (doğrulanmış) maçların tablolarını Polars'a alır.
2. `player_match_metrics`: her (maç, oyuncu) için ham SAYIMLAR (kill, opening, utility...).
3. `aggregate`: herhangi bir satır alt kümesini tek satıra ORANLARA çevirir.
   Oyuncu ve "lobi" (aynı maçlardaki diğer oyuncular) aynı fonksiyonla hesaplanır,
   böylece karşılaştırma adil olur.

Oranlar daima pay/payda olarak birlikte saklanır; rapor "%40 (12/30)" gösterebilsin.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import polars as pl

from director.analysis.maps import ZONES

# Silah kategorileri (kill dağılımı için)
WEAPON_CATEGORIES = {
    "rifle": ("ak47", "m4a1", "m4a1_silencer", "galilar", "famas", "aug", "sg556"),
    "awp": ("awp",),
    "scout": ("ssg08",),
    "smg": ("mp7", "mp9", "mac10", "p90", "ump45", "mp5sd", "bizon"),
    "pistol": ("glock", "usp_silencer", "hkp2000", "p250", "deagle", "tec9", "fiveseven",
               "elite", "cz75a", "revolver"),
    "heavy": ("mag7", "xm1014", "nova", "sawedoff", "negev", "m249", "g3sg1", "scar20"),
    "utility": ("hegrenade", "inferno"),
}

# Round içi zaman pencereleri (saniye, freeze bitişinden itibaren)
LURK_WINDOW = (5.0, 40.0)       # T: takımdan uzaklık ölçülen erken/orta faz
CT_SETUP_WINDOW = (15.0, 35.0)  # CT: oyuncunun "tuttuğu" alan
DEATH_EARLY, DEATH_LATE = 20.0, 60.0


@dataclass
class DemoData:
    """İşlenmiş maçların analiz için gereken tabloları."""

    rounds: pl.DataFrame
    roster: pl.DataFrame      # match_id, steam_id, faceit_id, nickname, faction
    kills: pl.DataFrame
    utility: pl.DataFrame
    positions: pl.DataFrame
    stats: pl.DataFrame       # demo_player_stats
    match_maps: pl.DataFrame  # match_id, map, date, parser_version
    damages: pl.DataFrame | None = None  # parser v2+ maçlar için

    def v2_match_ids(self) -> list[str]:
        """Wallbang ve hasar verisi olan (parser v2+) maçlar."""
        return self.match_maps.filter(pl.col("parser_version") >= 2)["match_id"].to_list()

    @property
    def match_count(self) -> int:
        return self.match_maps.height


def _q(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> pl.DataFrame:
    # DuckDB 2.x: ayrı imleç (açık sonuç kalmasın) + Arrow tablosu → Polars
    return pl.from_arrow(con.cursor().sql(sql, params=params).to_arrow_table())


def load_demo_data(con: duckdb.DuckDBPyConnection, map_name: str | None = None) -> DemoData:
    """Demo verisi DB'de olan (FACEIT ile doğrulanmış) maçları yükler."""
    maps = _q(
        con,
        """
        SELECT match_id, map, date, parser_version FROM matches
        WHERE demo_status = 'processed' AND (? IS NULL OR map = ?)
          AND match_id IN (SELECT DISTINCT match_id FROM rounds)
        ORDER BY date
        """,
        [map_name, map_name],
    )
    ids = maps["match_id"].to_list()

    def per_match(sql: str) -> pl.DataFrame:
        return _q(con, sql, [ids])

    return DemoData(
        rounds=per_match("SELECT * FROM rounds WHERE list_contains(?, match_id)"),
        roster=per_match(
            """
            SELECT s.match_id, p.steam_id, s.player_id AS faceit_id, s.nickname, s.faction
            FROM match_player_stats s JOIN players p ON p.faceit_id = s.player_id
            WHERE list_contains(?, s.match_id) AND p.steam_id IS NOT NULL
            """
        ),
        kills=per_match("SELECT * FROM kills WHERE list_contains(?, match_id)"),
        utility=per_match("SELECT * FROM utility WHERE list_contains(?, match_id)"),
        positions=per_match(
            "SELECT match_id, round_no, tick, time_in_round, player, side, x, y, place_name "
            "FROM positions_snapshot WHERE list_contains(?, match_id)"
        ),
        stats=per_match("SELECT * FROM demo_player_stats WHERE list_contains(?, match_id)"),
        match_maps=maps,
        damages=per_match("SELECT * FROM damages WHERE list_contains(?, match_id)"),
    )


# --- round bazında oyuncu tablosu --------------------------------------------

def player_rounds(d: DemoData) -> pl.DataFrame:
    """Her (maç, round, oyuncu): taraf, round kazanıldı mı, takımın alım tipi."""
    return (
        d.rounds.filter(pl.col("ct_faction").is_not_null())
        .join(d.roster, on="match_id")
        .with_columns(
            pl.when(pl.col("faction") == pl.col("ct_faction")).then(pl.lit("CT"))
            .otherwise(pl.lit("T")).alias("side")
        )
        .with_columns(
            (pl.col("winner_side") == pl.col("side")).alias("won"),
            pl.when(pl.col("side") == "CT").then(pl.col("ct_buy_type"))
            .otherwise(pl.col("t_buy_type")).alias("own_buy"),
            # MR12: her yarının ilk roundu pistol (uzatmalar hariç)
            pl.col("round_no").is_in([1, 13]).alias("is_pistol"),
        )
        .select("match_id", "round_no", "steam_id", "faction", "side", "won", "own_buy",
                "is_pistol")
    )


# --- konumdan türetilen sinyaller --------------------------------------------

def lurk_distance(d: DemoData) -> pl.DataFrame:
    """T tarafında oyuncunun, diğer T'lerin merkezine ortalama uzaklığı (maç başına).

    Lurker takımdan uzak oynar. En az 2 takım arkadaşı hayattayken ölçülür.
    """
    p = d.positions.filter(
        (pl.col("side") == "T")
        & pl.col("time_in_round").is_between(*LURK_WINDOW)
    )
    g = p.group_by("match_id", "round_no", "tick").agg(
        pl.col("x").sum().alias("sx"), pl.col("y").sum().alias("sy"), pl.len().alias("n")
    )
    return (
        p.join(g, on=["match_id", "round_no", "tick"])
        .filter(pl.col("n") >= 3)
        .with_columns(
            (((pl.col("sx") - pl.col("x")) / (pl.col("n") - 1) - pl.col("x")) ** 2
             + ((pl.col("sy") - pl.col("y")) / (pl.col("n") - 1) - pl.col("y")) ** 2)
            .sqrt().alias("dist")
        )
        .group_by("match_id", pl.col("player").alias("steam_id"))
        .agg(pl.col("dist").mean().alias("lurk_dist"))
    )


def ct_zone_behaviour(d: DemoData) -> pl.DataFrame:
    """CT'de tutulan alan istikrarı (anchor) ve site değiştirme (rotator) sayıları.

    - setup alanı: 15-35. saniyelerde en çok bulunduğu alan (A / B / Mid)
    - anchor round: setup alanı bir site (A/B) ve maç boyunca en sık tuttuğu alanla aynı
    - rotation round: round içinde (10. sn sonrası) hem A hem B alanında bulunmuş
      ya da Mid'den bir site'a geçmiş
    """
    zone_rows = [(m, place, zone) for m, zones in ZONES.items() for place, zone in zones.items()]
    if not zone_rows:
        return pl.DataFrame(schema={"match_id": pl.String, "steam_id": pl.String})
    zmap = pl.DataFrame(zone_rows, schema=["map", "place_name", "zone"], orient="row")

    p = (
        d.positions.filter((pl.col("side") == "CT") & (pl.col("time_in_round") >= 10))
        .join(d.match_maps.select("match_id", "map"), on="match_id")
        .join(zmap, on=["map", "place_name"])
    )
    setup = (
        p.filter(pl.col("time_in_round").is_between(*CT_SETUP_WINDOW))
        .group_by("match_id", "round_no", "player")
        .agg(pl.col("zone").mode().sort().first().alias("setup_zone"))
    )
    visited = p.group_by("match_id", "round_no", "player").agg(
        pl.col("zone").unique().alias("zones")
    )
    r = setup.join(visited, on=["match_id", "round_no", "player"]).with_columns(
        (
            (pl.col("zones").list.contains("A") & pl.col("zones").list.contains("B"))
            | ((pl.col("setup_zone") == "Mid")
               & (pl.col("zones").list.contains("A") | pl.col("zones").list.contains("B")))
        ).alias("rotated")
    )
    modal = r.group_by("match_id", "player").agg(
        pl.col("setup_zone").mode().sort().first().alias("home_zone")
    )
    return (
        r.join(modal, on=["match_id", "player"])
        .group_by("match_id", pl.col("player").alias("steam_id"))
        .agg(
            pl.len().alias("ct_setup_rounds"),
            ((pl.col("setup_zone") == pl.col("home_zone"))
             & pl.col("setup_zone").is_in(["A", "B"])).sum().alias("ct_anchor_rounds"),
            pl.col("rotated").sum().alias("ct_rotations"),
            pl.col("home_zone").first().alias("ct_home_zone"),
        )
    )


# --- (maç, oyuncu) ham sayımları -----------------------------------------------

def _weapon_category() -> pl.Expr:
    expr = pl.lit("other")
    for cat, weapons in reversed(WEAPON_CATEGORIES.items()):
        expr = pl.when(pl.col("weapon").is_in(weapons)).then(pl.lit(cat)).otherwise(expr)
    return expr


def player_match_metrics(d: DemoData) -> pl.DataFrame:
    """Her (maç, oyuncu) için ham sayımlar. Oranlar `aggregate` ile hesaplanır."""
    pr = player_rounds(d)
    keys = ["match_id", "steam_id"]

    base = pr.group_by(keys).agg(
        pl.col("faction").first(),
        pl.len().alias("rounds"),
        (pl.col("side") == "T").sum().alias("t_rounds"),
        (pl.col("side") == "CT").sum().alias("ct_rounds"),
        pl.col("won").sum().alias("wins"),
        ((pl.col("side") == "T") & pl.col("won")).sum().alias("t_wins"),
        ((pl.col("side") == "CT") & pl.col("won")).sum().alias("ct_wins"),
        pl.col("is_pistol").sum().alias("pistol_rounds"),
        (pl.col("is_pistol") & pl.col("won")).sum().alias("pistol_wins"),
    )

    # Kill'ler (takım arkadaşı öldürme sayılmaz)
    k = d.kills.filter(pl.col("attacker_side") != pl.col("victim_side")).with_columns(
        _weapon_category().alias("wcat")
    )
    kills = k.group_by("match_id", pl.col("attacker").alias("steam_id")).agg(
        pl.len().alias("kills"),
        (pl.col("attacker_side") == "T").sum().alias("t_kills"),
        (pl.col("attacker_side") == "CT").sum().alias("ct_kills"),
        (pl.col("is_opening") & (pl.col("attacker_side") == "T")).sum().alias("t_ok"),
        (pl.col("is_opening") & (pl.col("attacker_side") == "CT")).sum().alias("ct_ok"),
        pl.col("is_trade_kill").sum().alias("trade_kills"),
        pl.col("headshot").sum().alias("hs_kills"),
        *[(pl.col("wcat") == c).sum().alias(f"k_{c}") for c in WEAPON_CATEGORIES],
    )

    # Ölümler (bomba / düşme dahil, FACEIT gibi)
    deaths = d.kills.group_by("match_id", pl.col("victim").alias("steam_id")).agg(
        pl.len().alias("deaths"),
        (pl.col("victim_side") == "T").sum().alias("t_deaths"),
        (pl.col("victim_side") == "CT").sum().alias("ct_deaths"),
        (pl.col("is_opening") & (pl.col("victim_side") == "T")).sum().alias("t_od"),
        (pl.col("is_opening") & (pl.col("victim_side") == "CT")).sum().alias("ct_od"),
        pl.col("is_traded").sum().alias("traded_deaths"),
        (pl.col("time_in_round") < DEATH_EARLY).sum().alias("death_early"),
        pl.col("time_in_round").is_between(DEATH_EARLY, DEATH_LATE, closed="left").sum()
        .alias("death_mid"),
        (pl.col("time_in_round") >= DEATH_LATE).sum().alias("death_late"),
    )

    # İlk temas: oyuncunun round'daki ilk kill'i ya da ölümü
    contact = (
        pl.concat([
            d.kills.select("match_id", "round_no", pl.col("attacker").alias("steam_id"),
                           pl.col("attacker_side").alias("side"), "time_in_round"),
            d.kills.select("match_id", "round_no", pl.col("victim").alias("steam_id"),
                           pl.col("victim_side").alias("side"), "time_in_round"),
        ])
        .drop_nulls("steam_id")
        .group_by("match_id", "round_no", "steam_id")
        .agg(pl.col("side").first(), pl.col("time_in_round").min().alias("t_first"))
        .group_by("match_id", "steam_id")
        .agg(
            pl.col("t_first").filter(pl.col("side") == "T").sum().alias("t_contact_sum"),
            (pl.col("side") == "T").sum().alias("t_contact_n"),
            pl.col("t_first").filter(pl.col("side") == "CT").sum().alias("ct_contact_sum"),
            (pl.col("side") == "CT").sum().alias("ct_contact_n"),
        )
    )

    # Utility
    util = d.utility.group_by("match_id", pl.col("thrower").alias("steam_id")).agg(
        (pl.col("type") == "smoke").sum().alias("smokes"),
        (pl.col("type") == "flash").sum().alias("flashes"),
        (pl.col("type") == "he").sum().alias("hes"),
        (pl.col("type") == "molly").sum().alias("mollies"),
        pl.col("flashed_enemies").sum().alias("flashed_enemies"),
        pl.col("flashed_teammates").sum().alias("flashed_team"),
        pl.col("blind_duration").sum().alias("blind_time"),
        pl.col("damage").sum().alias("util_dmg"),
    )

    # awpy skor tablosundan: KAST, ADR, clutch, multikill, flash assist
    st = d.stats.select(
        "match_id", "steam_id", "rounds_played",
        (pl.col("kast") * pl.col("rounds_played") / 100).alias("kast_rounds"),
        (pl.col("adr") * pl.col("rounds_played")).alias("damage"),
        "flash_assists", "clutches_played", "clutches_won",
        (pl.col("multikill_3k") + pl.col("multikill_4k") + pl.col("multikill_5k"))
        .alias("multikill_rounds"),
    )

    out = base
    for extra in (kills, deaths, contact, util, st, lurk_distance(d), ct_zone_behaviour(d)):
        out = out.join(extra, on=keys, how="left")
    # Sayım kolonlarında boşluk = 0; ortalama/uzaklık kolonları boş kalır
    keep_null = {"lurk_dist", "ct_home_zone", "faction"}
    return out.with_columns(
        [pl.col(c).fill_null(0) for c in out.columns
         if c not in keys and c not in keep_null and out.schema[c].is_numeric()]
    )


# --- oranlar -----------------------------------------------------------------

@dataclass(frozen=True)
class Rate:
    """Pay / payda; rapor hem oranı hem örneklemi gösterebilsin diye."""

    num: float
    den: float

    @property
    def value(self) -> float | None:
        return self.num / self.den if self.den else None

    def pct(self) -> str:
        v = self.value
        return "-" if v is None else f"%{100 * v:.0f} ({self.num:.0f}/{self.den:.0f})"


def _s(df: pl.DataFrame, col: str) -> float:
    return float(df[col].sum()) if col in df.columns and df.height else 0.0


def aggregate(pm: pl.DataFrame) -> dict[str, Rate]:
    """(maç, oyuncu) satırlarını havuzlayıp oranlara çevirir (oyuncu ya da lobi için)."""
    s = lambda c: _s(pm, c)  # noqa: E731 — kısa yardımcı
    rounds, t_rounds, ct_rounds = s("rounds"), s("t_rounds"), s("ct_rounds")
    t_att, ct_att = s("t_ok") + s("t_od"), s("ct_ok") + s("ct_od")
    flashes = s("flashes")
    util_total = s("smokes") + flashes + s("hes") + s("mollies")

    # Lurk mesafesi: T round sayısıyla ağırlıklı ortalama
    lurk = pm.filter(pl.col("lurk_dist").is_not_null()) if "lurk_dist" in pm.columns else pm
    lurk_num = float((lurk["lurk_dist"] * lurk["t_rounds"]).sum()) if lurk.height else 0.0
    lurk_den = float(lurk["t_rounds"].sum()) if lurk.height else 0.0

    return {
        "matches": Rate(pm.height, 1),
        "rounds": Rate(rounds, 1),
        "round_win": Rate(s("wins"), rounds),
        "t_round_win": Rate(s("t_wins"), t_rounds),
        "ct_round_win": Rate(s("ct_wins"), ct_rounds),
        "pistol_win": Rate(s("pistol_wins"), s("pistol_rounds")),
        "kd": Rate(s("kills"), s("deaths")),
        "kpr": Rate(s("kills"), rounds),
        "dpr": Rate(s("deaths"), rounds),
        "adr": Rate(s("damage"), s("rounds_played")),
        "kast": Rate(s("kast_rounds"), s("rounds_played")),
        "hs": Rate(s("hs_kills"), s("kills")),
        "t_kpr": Rate(s("t_kills"), t_rounds),
        "ct_kpr": Rate(s("ct_kills"), ct_rounds),
        "t_dpr": Rate(s("t_deaths"), t_rounds),
        "ct_dpr": Rate(s("ct_deaths"), ct_rounds),
        "t_open_attempt": Rate(t_att, t_rounds),
        "t_open_win": Rate(s("t_ok"), t_att),
        "ct_open_attempt": Rate(ct_att, ct_rounds),
        "ct_open_win": Rate(s("ct_ok"), ct_att),
        "traded_death": Rate(s("traded_deaths"), s("deaths")),
        "trade_kill": Rate(s("trade_kills"), s("kills")),
        "awp_share": Rate(s("k_awp"), s("kills")),
        "sniper_share": Rate(s("k_awp") + s("k_scout"), s("kills")),
        "multikill": Rate(s("multikill_rounds"), rounds),
        "clutch_win": Rate(s("clutches_won"), s("clutches_played")),
        "util_per_round": Rate(util_total, rounds),
        "flashes_per_round": Rate(flashes, rounds),
        "smokes_per_round": Rate(s("smokes"), rounds),
        "enemies_per_flash": Rate(s("flashed_enemies"), flashes),
        "team_flash_per_flash": Rate(s("flashed_team"), flashes),
        "flash_assists_per_round": Rate(s("flash_assists"), rounds),
        "util_dmg_per_round": Rate(s("util_dmg"), rounds),
        "t_first_contact": Rate(s("t_contact_sum"), s("t_contact_n")),
        "ct_first_contact": Rate(s("ct_contact_sum"), s("ct_contact_n")),
        "death_early": Rate(s("death_early"), s("deaths")),
        "death_late": Rate(s("death_late"), s("deaths")),
        "lurk_dist": Rate(lurk_num, lurk_den),
        "ct_anchor": Rate(s("ct_anchor_rounds"), s("ct_setup_rounds")),
        "ct_rotation": Rate(s("ct_rotations"), s("ct_setup_rounds")),
    }


# --- oyuncuya özel kırılımlar ------------------------------------------------

def weapon_breakdown(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Oyuncunun silah bazında kill sayısı ve payı."""
    k = d.kills.filter(
        (pl.col("attacker") == steam_id) & (pl.col("attacker_side") != pl.col("victim_side"))
    )
    return (
        k.group_by("weapon").agg(pl.len().alias("kills"), pl.col("headshot").sum().alias("hs"))
        .with_columns((pl.col("kills") / k.height).alias("share"))
        .sort("kills", descending=True)
    )


def place_breakdown(d: DemoData, steam_id: str, kind: str) -> pl.DataFrame:
    """Oyuncunun ölüm ('death') ya da kill ('kill') yerleri, tarafa göre."""
    if kind == "death":
        df = d.kills.filter(pl.col("victim") == steam_id).select(
            pl.col("victim_side").alias("side"), pl.col("victim_place").alias("place"),
            "is_traded",
        )
    else:
        df = d.kills.filter(
            (pl.col("attacker") == steam_id) & (pl.col("attacker_side") != pl.col("victim_side"))
        ).select(pl.col("attacker_side").alias("side"), pl.col("attacker_place").alias("place"),
                 pl.lit(None, pl.Boolean).alias("is_traded"))
    totals = df.group_by("side").agg(pl.len().alias("side_total"))
    return (
        df.group_by("side", "place")
        .agg(pl.len().alias("n"), pl.col("is_traded").sum().alias("traded"))
        .join(totals, on="side")
        .with_columns((pl.col("n") / pl.col("side_total")).alias("share"))
        .sort(["side", "n"], descending=[False, True])
    )


def economy_breakdown(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Takımın alım tipine göre: round sayısı, kazanma ve oyuncunun round başı kill'i."""
    pr = player_rounds(d).filter(pl.col("steam_id") == steam_id)
    k = (
        d.kills.filter((pl.col("attacker") == steam_id)
                       & (pl.col("attacker_side") != pl.col("victim_side")))
        .group_by("match_id", "round_no").agg(pl.len().alias("k"))
    )
    order = {"pistol": 0, "eco": 1, "force": 2, "full": 3}
    return (
        pr.join(k, on=["match_id", "round_no"], how="left")
        .with_columns(pl.col("k").fill_null(0))
        .group_by("side", "own_buy")
        .agg(pl.len().alias("rounds"), pl.col("won").sum().alias("wins"),
             pl.col("k").sum().alias("kills"))
        .with_columns(pl.col("own_buy").replace_strict(order, default=9).alias("_o"))
        .sort("side", "_o")
        .drop("_o")
    )


def weapon_place_breakdown(d: DemoData, steam_id: str) -> pl.DataFrame:
    """Oyuncunun bölge × silah performansı: sadece kill değil, hasar ve duvar arkası da.

    Kolonlar:
    - kills / openings: o bölgeden o silahla (tüm maçlar)
    - wallbangs: motorun "mermi yüzey deldi" dediği kill'ler (parser v2+ maçlar)
    - deaths_here: oyuncunun o bölgede ölümü (silahtan bağımsız; bölgedeki bedel)
    - damage / hits / wall_damage: hasar, isabet ve tahmini duvar arkası hasar (v2+ maçlar)
    """
    weapon_cat = _weapon_category()
    k = (
        d.kills.filter((pl.col("attacker") == steam_id)
                       & (pl.col("attacker_side") != pl.col("victim_side")))
        .with_columns(weapon_cat.alias("wcat"))
        .group_by(pl.col("attacker_place").alias("place"), "wcat")
        .agg(
            pl.len().alias("kills"),
            pl.col("is_opening").sum().alias("openings"),
            (pl.col("penetrated").fill_null(0) > 0).sum().alias("wallbangs"),
        )
    )
    deaths = (
        d.kills.filter(pl.col("victim") == steam_id)
        .group_by(pl.col("victim_place").alias("place"))
        .agg(pl.len().alias("deaths_here"))
    )
    out = k
    if d.damages is not None and d.damages.height:
        dmg = (
            d.damages.filter((pl.col("attacker") == steam_id)
                             & (pl.col("attacker_side") != pl.col("victim_side")))
            .with_columns(weapon_cat.alias("wcat"))
            .group_by(pl.col("attacker_place").alias("place"), "wcat")
            .agg(
                pl.col("dmg_health").sum().alias("damage"),
                pl.len().alias("hits"),
                pl.col("dmg_health").filter(pl.col("through_wall")).sum().alias("wall_damage"),
                pl.col("through_wall").sum().alias("wall_hits"),
            )
        )
        out = out.join(dmg, on=["place", "wcat"], how="full", coalesce=True)
    return (
        out.join(deaths, on="place", how="left")
        .with_columns(pl.col(c).fill_null(0) for c in ("kills", "openings", "wallbangs"))
        .rename({"wcat": "weapon"})
        .sort("kills", descending=True)
    )
