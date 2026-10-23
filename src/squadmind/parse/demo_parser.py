"""awpy ile CS2 demosunu okuyup DB tablolarına uygun Polars DataFrame'lerine çevirir.

Bu modül veritabanını bilmez: sadece demo → tablolar. Maç ID'si, dosya taşıma ve
DB yazma işleri `squadmind.ingest.demos` içinde.

Kolon adları awpy 3.x'in gerçek çıktısından alındı (bir FACEIT Dust2 demosu ile
doğrulandı). Steam ID'ler DB'deki players.steam_id ile eşleşsin diye metne çevrilir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import polars as pl
from awpy import Demo, VisibilityChecker

from squadmind.parse.damage import cap_damage

if TYPE_CHECKING:  # yalnız tip ipucu için (döngüsel içe aktarma olmasın)
    from squadmind.parse.package import PackageDemo

# Parser sürümü: DB'deki hangi maçın hangi alanlara sahip olduğunu ayırt etmek için.
# v1: ilk sürüm · v2: kill'de wallbang (penetrated) + hasar tablosu (duvar arkası tespiti)
# v3: utility (HE / molotof) hasarı kurbanın kalan canına göre kırpılır
PARSER_VERSION = 3

# Görüş kontrolü: ayakta / çömelik göz yüksekliği ve kurbanın baş / gövde / ayak noktası
# (Hammer birimi). Herhangi bir göz → hedef çizgisi açıksa vuruş "direkt" sayılır.
# 4 demoda motorun wallbang kill bilgisiyle ölçüldü: wallbang'lerin %73'ünü yakalar,
# direkt vuruşların %3'ünü yanlışlıkla duvar arkası sayar (tek çizgide bu %14'tü).
EYE_HEIGHTS = (64.0, 46.0)
TARGET_HEIGHTS = (64.0, 40.0, 12.0)

# Mermi atmayan hasar kaynakları: duvar arkası kontrolü bunlar için anlamsız
NON_BULLET_WEAPONS = {"hegrenade", "inferno", "molotov", "incgrenade", "planted_c4", "world",
                      "knife", "knife_t", "taser"}

# awpy taraf adları → raporlarda kullandığımız kısa adlar
SIDE_MAP = {"counter-terrorist": "CT", "terrorist": "T"}

# weapon_fire olayındaki silah adı → grenade trajektorisindeki tip
THROW_WEAPON_TO_TYPE = {
    "weapon_smokegrenade": "smoke",
    "weapon_flashbang": "flashbang",
    "weapon_hegrenade": "he",
    "weapon_molotov": "molotov",
    "weapon_incgrenade": "molotov",
    "weapon_decoy": "decoy",
}

# Molotof / yangın hasarı demoda bu silah adlarıyla geçer
FIRE_DAMAGE_WEAPONS = ("inferno", "molotov", "incgrenade")

# Pozisyon örnekleme aralığı (CLAUDE.md: her 2 saniyede bir)
SNAPSHOT_SECONDS = 2.0

# Atış ile mermi başlangıcı arasında izin verilen en fazla gecikme (tick)
THROW_MATCH_WINDOW = 64

# Utility'nin düştüğü yere bölge adı verirken izin verilen en uzak mesafe (Hammer birimi)
PLACE_MAX_DISTANCE = 600.0


@dataclass
class DemoTables:
    """Bir demodan çıkan, DB'ye yazılmaya hazır tablolar (match_id hariç)."""

    map_name: str
    tick_rate: float
    players: pl.DataFrame  # steamid, name, side, team_clan_name
    rounds: pl.DataFrame
    kills: pl.DataFrame
    utility: pl.DataFrame
    positions: pl.DataFrame
    player_stats: pl.DataFrame
    damages: pl.DataFrame | None = None


# --- yardımcılar -------------------------------------------------------------

def _side(col: str) -> pl.Expr:
    """'counter-terrorist' → 'CT', 'terrorist' → 'T', diğerleri null."""
    return pl.col(col).replace_strict(SIDE_MAP, default=None, return_dtype=pl.String)


def _steamid(col: str) -> pl.Expr:
    """UInt64 steam id → metin; 0 (bot / bilinmeyen) → null."""
    return pl.when(pl.col(col) > 0).then(pl.col(col).cast(pl.String)).otherwise(None)


def round_windows(rounds: pl.DataFrame) -> pl.DataFrame:
    """Canlı round aralıkları: freeze bitişi → round'un resmi bitişi.

    Isınma (round 1'in freeze bitişinden önce) ve bıçak roundları bu aralıkların
    dışında kalır; böylece ısınma kill'leri istatistiğe girmez.
    """
    return (
        rounds.filter(~pl.col("is_knife_round").fill_null(False))
        .filter(pl.col("freeze_end_tick").is_not_null())
        .select(
            pl.col("round_num").cast(pl.Int32).alias("round_no"),
            pl.col("freeze_end_tick").cast(pl.Int64),
            pl.col("end_tick").cast(pl.Int64),
            # Round sonrası (exit frag) kill'ler de FACEIT skoruna sayılır
            pl.coalesce("official_end_tick", "end_tick").cast(pl.Int64).alias("window_end"),
        )
        .sort("freeze_end_tick")
    )


def assign_round(df: pl.DataFrame, windows: pl.DataFrame, tick_col: str = "tick") -> pl.DataFrame:
    """Her satıra round_no ve freeze_end_tick ekler; canlı round dışındakileri atar."""
    return (
        df.with_columns(pl.col(tick_col).cast(pl.Int64).alias("_t"))
        .sort("_t")
        .join_asof(
            windows.select("round_no", "freeze_end_tick", "window_end"),
            left_on="_t",
            right_on="freeze_end_tick",
            strategy="backward",
        )
        .filter(pl.col("round_no").is_not_null() & (pl.col("_t") <= pl.col("window_end")))
        .drop("_t", "window_end")
    )


def nearest_place(
    points: np.ndarray,
    ref_points: np.ndarray,
    ref_places: list[str],
    max_distance: float = PLACE_MAX_DISTANCE,
    z_weight: float = 0.5,
) -> list[str | None]:
    """Her nokta için en yakın referans noktanın bölge adını döner.

    Referans: oyuncuların bilinen konum + bölge adları. Flash'lar havada patladığı
    için yükseklik (z) farkı daha az ağırlıklı sayılır.
    """
    if len(points) == 0 or len(ref_points) == 0:
        return [None] * len(points)
    scale = np.array([1.0, 1.0, z_weight])
    ref = ref_points * scale
    out: list[str | None] = []
    # Bellek patlamasın diye parça parça hesapla
    for start in range(0, len(points), 256):
        chunk = points[start : start + 256] * scale
        d2 = ((chunk[:, None, :] - ref[None, :, :]) ** 2).sum(axis=2)
        idx = d2.argmin(axis=1)
        best = d2[np.arange(len(chunk)), idx]
        out.extend(
            ref_places[i] if b <= max_distance**2 else None for i, b in zip(idx, best)
        )
    return out


# --- tablolar ----------------------------------------------------------------

def build_rounds(rounds: pl.DataFrame, economy: pl.DataFrame, bomb: pl.DataFrame,
                 windows: pl.DataFrame) -> pl.DataFrame:
    """Round tablosu: kazanan, sebep, iki tarafın alım tipi ve bomba sitesi."""
    eco = economy.with_columns(_side("side").alias("s")).filter(pl.col("s").is_not_null())
    ct = eco.filter(pl.col("s") == "CT").select(
        pl.col("round_num").cast(pl.Int32).alias("round_no"),
        pl.col("buy_type").alias("ct_buy_type"),
        pl.col("equipment_value").alias("ct_equipment"),
    )
    t = eco.filter(pl.col("s") == "T").select(
        pl.col("round_num").cast(pl.Int32).alias("round_no"),
        pl.col("buy_type").alias("t_buy_type"),
        pl.col("equipment_value").alias("t_equipment"),
    )

    # Round başına ilk tamamlanan plant (round bittikten sonraki plant'ler sayılmaz)
    plants = assign_round(bomb.filter(pl.col("event") == "finish_plant"), windows)
    plants = plants.join(windows.select("round_no", "end_tick"), on="round_no").filter(
        pl.col("tick") <= pl.col("end_tick")
    )
    plants = plants.sort("tick").group_by("round_no").agg(
        pl.col("bombsite").first().alias("bomb_site"),
        pl.col("tick").first().cast(pl.Int64).alias("plant_tick"),
    )

    return (
        rounds.with_columns(pl.col("round_num").cast(pl.Int32).alias("round_no"))
        .join(windows.select("round_no"), on="round_no", how="semi")
        .select(
            "round_no",
            _side("winner_side").alias("winner_side"),
            pl.col("reason_name").alias("reason"),
            pl.col("freeze_end_tick").cast(pl.Int64),
            pl.col("end_tick").cast(pl.Int64),
        )
        .join(ct, on="round_no", how="left")
        .join(t, on="round_no", how="left")
        .join(plants, on="round_no", how="left")
        .sort("round_no")
    )


def build_kills(kills: pl.DataFrame, places: pl.DataFrame, windows: pl.DataFrame,
                tick_rate: float) -> pl.DataFrame:
    """Kill tablosu: bölge adları, round içi zaman, opening ve trade bilgisi."""
    k = assign_round(kills, windows).sort("tick")
    k = k.with_columns(
        pl.col("tick").cast(pl.Int64),
        ((pl.col("tick") - pl.col("freeze_end_tick")) / tick_rate).alias("time_in_round"),
        # Round'un ilk kill'i = opening duel
        (pl.int_range(pl.len()).over("round_no") == 0).alias("is_opening"),
    )
    # Ölüm anındaki bölge adları (oyuncunun kendi "son bölge" alanı)
    att = places.rename({"steamid": "attacker_steamid", "place": "attacker_place"})
    vic = places.rename({"steamid": "victim_steamid", "place": "victim_place"})
    k = k.join(att, on=["tick", "attacker_steamid"], how="left").join(
        vic, on=["tick", "victim_steamid"], how="left"
    )
    return k.select(
        "round_no",
        "tick",
        "time_in_round",
        _steamid("attacker_steamid").alias("attacker"),
        pl.col("attacker_name"),
        _side("attacker_side").alias("attacker_side"),
        _steamid("victim_steamid").alias("victim"),
        pl.col("victim_name"),
        _side("victim_side").alias("victim_side"),
        _steamid("assister_steamid").alias("assister"),
        "weapon",
        "headshot",
        pl.col("assist_flash").alias("flash_assist"),
        pl.col("thrusmoke").alias("thru_smoke"),
        "attacker_place",
        "victim_place",
        pl.col("attacker_x").cast(pl.Float64),
        pl.col("attacker_y").cast(pl.Float64),
        pl.col("attacker_z").cast(pl.Float64),
        pl.col("victim_x").cast(pl.Float64),
        pl.col("victim_y").cast(pl.Float64),
        pl.col("victim_z").cast(pl.Float64),
        "is_opening",
        # Kaç yüzeyi delip geçti (0 = direkt; >0 = wallbang, ör. kapı arkasından)
        pl.col("penetrated").cast(pl.Int32),
        "noscope",
        # Bu kill bir takım arkadaşının ölümünün intikamı mı
        pl.col("is_trade").alias("is_trade_kill"),
        # Bu ölüm kısa sürede trade edildi mi
        pl.col("victim_traded").alias("is_traded"),
    )


def through_wall(df: pl.DataFrame, vis: VisibilityChecker | None) -> pl.Series:
    """Tahmini duvar / kapı arkası vuruş: saldırgan ile kurban arasındaki tüm çizgiler kapalı mı.

    Motor hasar olayında delme bilgisi vermez (kill'de verir: `penetrated`); bu yüzden
    harita çarpışma modeliyle kontrol ederiz. Smoke görüşü engellemez (geometri değil).
    Utility / dünya hasarı → None. Doğruluk için EYE_HEIGHTS açıklamasına bak.
    """
    if vis is None or df.height == 0:
        return pl.Series("through_wall", [None] * df.height, dtype=pl.Boolean)
    out: list[bool | None] = []
    for ax, ay, az, vx, vy, vz, weapon in df.select(
        "attacker_x", "attacker_y", "attacker_z", "victim_x", "victim_y", "victim_z", "weapon"
    ).iter_rows():
        if ax is None or vx is None or weapon in NON_BULLET_WEAPONS:
            out.append(None)
            continue
        visible = any(
            vis.is_visible((ax, ay, az + eye), (vx, vy, vz + target))
            for eye in EYE_HEIGHTS for target in TARGET_HEIGHTS
        )
        out.append(not visible)
    return pl.Series("through_wall", out, dtype=pl.Boolean)


def build_damages(damages: pl.DataFrame, places: pl.DataFrame, windows: pl.DataFrame,
                  tick_rate: float, vis: VisibilityChecker | None) -> pl.DataFrame:
    """Her hasar olayı: kim, kime, hangi silahla, nereden nereye, duvar arkasından mı."""
    d = assign_round(damages, windows).sort("tick").with_columns(pl.col("tick").cast(pl.Int64))
    att = places.rename({"steamid": "attacker_steamid", "place": "attacker_place"})
    vic = places.rename({"steamid": "victim_steamid", "place": "victim_place"})
    d = d.join(att, on=["tick", "attacker_steamid"], how="left").join(
        vic, on=["tick", "victim_steamid"], how="left"
    )
    d = d.with_columns(through_wall(d, vis))
    return d.select(
        "round_no",
        "tick",
        ((pl.col("tick") - pl.col("freeze_end_tick")) / tick_rate).alias("time_in_round"),
        _steamid("attacker_steamid").alias("attacker"),
        _side("attacker_side").alias("attacker_side"),
        _steamid("victim_steamid").alias("victim"),
        _side("victim_side").alias("victim_side"),
        "weapon",
        pl.col("dmg_health").cast(pl.Int32),
        pl.col("dmg_armor").cast(pl.Int32),
        pl.col("hitgroup_name").alias("hitgroup"),
        "attacker_place",
        "victim_place",
        "through_wall",
    )


def resolve_projectiles(grenades: pl.DataFrame, shots: pl.DataFrame) -> pl.DataFrame:
    """Her grenade mermisinin başlangıç/bitiş noktası ve atan oyuncusu.

    awpy çoğu mermide atanı boş bırakıyor. Atanı, aynı tipte ve mermi başlamadan
    hemen önceki `weapon_fire` olayları arasından konumu en yakın olanla buluruz.
    """
    proj = (
        grenades.sort("tick")
        .group_by("entity_id", "entity_serial", "type")
        .agg(
            pl.col("tick").first().cast(pl.Int64).alias("start_tick"),
            pl.col("tick").last().cast(pl.Int64).alias("end_tick"),
            pl.col("x").first().alias("sx"), pl.col("y").first().alias("sy"),
            pl.col("z").first().alias("sz"),
            pl.col("x").last().alias("ex"), pl.col("y").last().alias("ey"),
            pl.col("z").last().alias("ez"),
            pl.col("thrower_steamid").drop_nulls().first().alias("thrower_steamid"),
            pl.col("thrower_name").drop_nulls().first().alias("thrower_name"),
            pl.col("thrower_side").drop_nulls().first().alias("thrower_side"),
        )
        .with_row_index("pid")
    )

    throws = shots.filter(pl.col("weapon").is_in(list(THROW_WEAPON_TO_TYPE))).select(
        pl.col("weapon").replace_strict(THROW_WEAPON_TO_TYPE).alias("type"),
        pl.col("tick").cast(pl.Int64).alias("throw_tick"),
        pl.col("steamid").alias("t_steamid"),
        pl.col("name").alias("t_name"),
        pl.col("side").alias("t_side"),
        pl.col("x").alias("tx"), pl.col("y").alias("ty"), pl.col("z").alias("tz"),
    )

    missing = proj.filter(pl.col("thrower_steamid").is_null())
    matched = (
        missing.select("pid", "type", "start_tick", "sx", "sy", "sz")
        .join(throws, on="type")
        .filter(
            (pl.col("throw_tick") <= pl.col("start_tick"))
            & (pl.col("throw_tick") >= pl.col("start_tick") - THROW_MATCH_WINDOW)
        )
        .with_columns(
            ((pl.col("sx") - pl.col("tx")) ** 2 + (pl.col("sy") - pl.col("ty")) ** 2
             + (pl.col("sz") - pl.col("tz")) ** 2).alias("d2")
        )
        .sort("d2")
        .group_by("pid")
        .first()
        .select("pid", "t_steamid", "t_name", "t_side")
    )

    return (
        proj.join(matched, on="pid", how="left")
        .with_columns(
            pl.coalesce("thrower_steamid", "t_steamid").alias("thrower_steamid"),
            pl.coalesce("thrower_name", "t_name").alias("thrower_name"),
            pl.coalesce("thrower_side", "t_side").alias("thrower_side"),
        )
        .drop("t_steamid", "t_name", "t_side")
    )


def _attach_by_entity(events: pl.DataFrame, proj: pl.DataFrame, proj_type: str) -> pl.DataFrame:
    """Patlama olayını (entity id ile) kendi mermisine bağlar: atanı alır.

    Entity id'ler round'lar arasında tekrar kullanıldığı için olaydan önce başlayan
    en yakın mermi seçilir.
    """
    p = proj.filter(pl.col("type") == proj_type).select(
        "entity_id", "start_tick", "thrower_steamid", "thrower_name", "thrower_side"
    )
    return (
        events.with_row_index("eid")
        .join(p, on="entity_id", how="left")
        .filter(pl.col("start_tick").is_null() | (pl.col("start_tick") <= pl.col("tick")))
        .sort("start_tick", descending=True, nulls_last=True)
        .group_by("eid", maintain_order=True)
        .first()
        .drop("eid", "start_tick")
    )


def _detonations(events, name: str) -> pl.DataFrame:
    """Ham detonate olayını (metin kolonlu) sayısal tabloya çevirir."""
    if name not in events:
        return pl.DataFrame(
            schema={"tick": pl.Int64, "entity_id": pl.Int32,
                    "x": pl.Float64, "y": pl.Float64, "z": pl.Float64}
        )
    return events[name].select(
        pl.col("tick").cast(pl.Int64),
        pl.col("entityid").cast(pl.Int32).alias("entity_id"),
        pl.col("x").cast(pl.Float64),
        pl.col("y").cast(pl.Float64),
        pl.col("z").cast(pl.Float64),
    )


def _no_effect_columns(df: pl.DataFrame, type_name: str) -> pl.DataFrame:
    """Etki kolonları olmayan utility tipleri için boş kolonlar ekler."""
    return df.with_columns(
        pl.lit(type_name).alias("type"),
        pl.lit(None, pl.Int32).alias("flashed_enemies"),
        pl.lit(None, pl.Int32).alias("flashed_teammates"),
        pl.lit(None, pl.Float64).alias("blind_duration"),
    )


def build_utility(demo: Demo | PackageDemo, proj: pl.DataFrame, windows: pl.DataFrame,
                  ref_points: np.ndarray, ref_places: list[str],
                  tick_rate: float) -> pl.DataFrame:
    """Smoke / flash / HE / molotof kullanımları: kim, nereye, ne etki."""
    events = demo.events
    frames: list[pl.DataFrame] = []

    # --- smoke: awpy'nin smoke tablosu + mermi eşleşmesi
    smokes = demo.smokes.select(
        pl.col("start_tick").cast(pl.Int64).alias("tick"),
        pl.col("entity_id"),
        pl.col("x").cast(pl.Float64), pl.col("y").cast(pl.Float64), pl.col("z").cast(pl.Float64),
    )
    s = _no_effect_columns(_attach_by_entity(smokes, proj, "smoke"), "smoke").with_columns(
        pl.lit(None, pl.Float64).alias("damage")
    )
    frames.append(s)

    # --- flash: patlama + kör edilen oyuncular
    flashes = _attach_by_entity(
        _detonations(events, "flashbang_detonate"), proj, "flashbang"
    ).with_row_index("fid")
    blinds = demo.blinds.filter(pl.col("victim_steamid") != pl.col("attacker_steamid")).select(
        pl.col("tick").cast(pl.Int64).alias("btick"),
        pl.col("attacker_steamid").alias("thrower_steamid"),
        (pl.col("victim_side") != pl.col("attacker_side")).alias("is_enemy"),
        pl.col("duration").cast(pl.Float64),
    )
    flash_fx = (
        flashes.select("fid", "tick", "thrower_steamid")
        .join(blinds, on="thrower_steamid")
        # Körlük, patlamayla aynı anda (birkaç tick tolerans) başlar
        .filter((pl.col("btick") - pl.col("tick")).abs() <= 4)
        .group_by("fid")
        .agg(
            pl.col("is_enemy").sum().cast(pl.Int32).alias("flashed_enemies"),
            (~pl.col("is_enemy")).sum().cast(pl.Int32).alias("flashed_teammates"),
            pl.when(pl.col("is_enemy")).then(pl.col("duration")).otherwise(0.0).sum()
            .alias("blind_duration"),
        )
    )
    f = (
        flashes.join(flash_fx, on="fid", how="left")
        .drop("fid")
        .with_columns(
            pl.lit("flash").alias("type"),
            pl.col("flashed_enemies").fill_null(0),
            pl.col("flashed_teammates").fill_null(0),
            pl.col("blind_duration").fill_null(0.0),
            pl.lit(None, pl.Float64).alias("damage"),
        )
    )
    frames.append(f)

    # --- HE: patlama + rakibe verilen hasar
    hes = _attach_by_entity(
        _detonations(events, "hegrenade_detonate"), proj, "he"
    ).with_row_index("hid")
    # Hasar kalan cana göre kırpılır: 10 canı kalan rakibe HE 10 hasar sayılır
    capped = cap_damage(assign_round(demo.damages, windows), ["round_no", "victim_steamid"],
                        out="dmg_capped")
    he_dmg = capped.filter(pl.col("weapon") == "hegrenade").select(
        pl.col("tick").cast(pl.Int64).alias("dtick"),
        pl.col("attacker_steamid").alias("thrower_steamid"),
        pl.when(pl.col("victim_side") != pl.col("attacker_side"))
        .then(pl.col("dmg_capped")).otherwise(0).cast(pl.Float64).alias("dmg"),
    )
    he_fx = (
        hes.select("hid", "tick", "thrower_steamid")
        .join(he_dmg, on="thrower_steamid")
        .filter((pl.col("dtick") >= pl.col("tick")) & (pl.col("dtick") <= pl.col("tick") + 4))
        .group_by("hid")
        .agg(pl.col("dmg").sum().alias("damage"))
    )
    h = _no_effect_columns(
        hes.join(he_fx, on="hid", how="left").drop("hid"), "he"
    ).with_columns(pl.col("damage").fill_null(0.0))
    frames.append(h)

    # --- molotof: yangın başlangıcını, bitiş noktası en yakın molotof mermisiyle eşle
    fires = demo.fires.with_row_index("mid").select(
        "mid",
        pl.col("start_tick").cast(pl.Int64).alias("tick"),
        pl.col("end_tick").cast(pl.Int64).alias("fire_end"),
        pl.col("x").cast(pl.Float64), pl.col("y").cast(pl.Float64), pl.col("z").cast(pl.Float64),
        pl.col("thrower_steamid").alias("fire_thrower"),
        pl.col("thrower_name").alias("fire_thrower_name"),
        pl.col("thrower_side").alias("fire_thrower_side"),
    )
    molly_proj = proj.filter(pl.col("type") == "molotov").select(
        "end_tick", "ex", "ey", "thrower_steamid", "thrower_name", "thrower_side"
    )
    molly_match = (
        fires.select("mid", "tick", "x", "y")
        .join(molly_proj, how="cross")
        .filter((pl.col("end_tick") - pl.col("tick")).abs() <= 16)
        .with_columns(((pl.col("x") - pl.col("ex")) ** 2 + (pl.col("y") - pl.col("ey")) ** 2)
                      .alias("d2"))
        .sort("d2")
        .group_by("mid")
        .first()
        .select("mid", "thrower_steamid", "thrower_name", "thrower_side")
    )
    m = fires.join(molly_match, on="mid", how="left").with_columns(
        # awpy atanı bulduysa onu kullan, yoksa eşleşmeden geleni
        pl.coalesce("fire_thrower", "thrower_steamid").alias("thrower_steamid"),
        pl.coalesce("fire_thrower_name", "thrower_name").alias("thrower_name"),
        pl.coalesce("fire_thrower_side", "thrower_side").alias("thrower_side"),
    )
    fire_dmg = capped.filter(pl.col("weapon").is_in(FIRE_DAMAGE_WEAPONS)).select(
        pl.col("tick").cast(pl.Int64).alias("dtick"),
        pl.col("attacker_steamid").alias("thrower_steamid"),
        pl.when(pl.col("victim_side") != pl.col("attacker_side"))
        .then(pl.col("dmg_capped")).otherwise(0).cast(pl.Float64).alias("dmg"),
    )
    m_fx = (
        m.select("mid", "tick", "fire_end", "thrower_steamid")
        .join(fire_dmg, on="thrower_steamid")
        .filter((pl.col("dtick") >= pl.col("tick")) & (pl.col("dtick") <= pl.col("fire_end")))
        .group_by("mid")
        .agg(pl.col("dmg").sum().alias("damage"))
    )
    m = _no_effect_columns(
        m.join(m_fx, on="mid", how="left").drop(
            "mid", "fire_end", "fire_thrower", "fire_thrower_name", "fire_thrower_side"
        ),
        "molly",
    ).with_columns(pl.col("damage").fill_null(0.0))
    frames.append(m)

    cols = ["tick", "x", "y", "z", "thrower_steamid", "thrower_name", "thrower_side", "type",
            "flashed_enemies", "flashed_teammates", "blind_duration", "damage"]
    util = pl.concat([fr.select(cols) for fr in frames], how="vertical_relaxed")
    util = assign_round(util, windows).sort("tick")

    # Düştüğü yerin bölge adı
    pts = util.select("x", "y", "z").to_numpy().astype(np.float64)
    util = util.with_columns(
        pl.Series("land_place", nearest_place(pts, ref_points, ref_places), dtype=pl.String)
    )

    return util.select(
        "round_no",
        pl.col("tick").cast(pl.Int64),
        ((pl.col("tick") - pl.col("freeze_end_tick")) / tick_rate).alias("time_in_round"),
        _steamid("thrower_steamid").alias("thrower"),
        "thrower_name",
        _side("thrower_side").alias("thrower_side"),
        "type", "x", "y", "z", "land_place",
        "flashed_enemies", "flashed_teammates", "blind_duration", "damage",
    )


def build_positions(snapshots: pl.DataFrame, places: pl.DataFrame, windows: pl.DataFrame,
                    tick_rate: float) -> pl.DataFrame:
    """Canlı round içinde, hayattaki oyuncuların 2 saniyelik konum örnekleri."""
    s = assign_round(snapshots, windows)
    # Round bitişinden sonraki (post-round) örnekleri at: taktik analizi için gürültü
    s = s.join(windows.select("round_no", "end_tick"), on="round_no").filter(
        (pl.col("tick") <= pl.col("end_tick")) & (pl.col("health") > 0)
    )
    s = s.with_columns(pl.col("tick").cast(pl.Int64)).join(
        places, on=["tick", "steamid"], how="left"
    )
    return s.select(
        "round_no",
        "tick",
        ((pl.col("tick") - pl.col("freeze_end_tick")) / tick_rate).alias("time_in_round"),
        _steamid("steamid").alias("player"),
        pl.col("name").alias("player_name"),
        _side("side").alias("side"),
        pl.col("x").cast(pl.Float64),
        pl.col("y").cast(pl.Float64),
        pl.col("z").cast(pl.Float64),
        pl.col("place").alias("place_name"),
        "health",
        "money",
        "equipment_value",
        "active_weapon",
    ).sort("tick", "player")


def build_player_stats(stats: pl.DataFrame) -> pl.DataFrame:
    """awpy'nin oyuncu istatistikleri (KAST, opening, trade, clutch, utility)."""
    return stats.filter(pl.col("steamid") > 0).select(
        _steamid("steamid").alias("steam_id"),
        pl.col("name"),
        "rounds_played", "kills", "deaths", "assists", "flash_assists",
        "headshot_kills",
        pl.col("adr").cast(pl.Float64),
        pl.col("kast").cast(pl.Float64),
        "opening_kills", "opening_deaths", "traded_deaths",
        "multikill_2k", "multikill_3k", "multikill_4k", "multikill_5k",
        "clutches_played", "clutches_won",
        "utility_damage", "flashes_thrown", "enemies_flashed",
        pl.col("flash_duration_dealt").cast(pl.Float64),
    )


# --- ana giriş ---------------------------------------------------------------

def parse_demo(path: Path | str) -> DemoTables:
    """Açılmış bir .dem dosyasını okuyup tüm tabloları üretir."""
    return tables_from_demo(Demo(Path(path)))


def tables_from_demo(demo: Demo | PackageDemo) -> DemoTables:
    """awpy `Demo` (ya da aynı arayüzlü `PackageDemo`) nesnesinden tüm tabloları üretir.

    Kullanılan arayüz: header, tick_rate, rounds, round_economy, bomb, kills, damages,
    smokes, fires, blinds, grenades, shots, players, stats, events, snapshots(seconds=...),
    ticks(["m_szLastPlaceName"], players_only=True).
    """
    tick_rate = demo.tick_rate
    windows = round_windows(demo.rounds)

    # Her tick için oyuncunun bulunduğu bölge adı (ör. "LongA")
    places = demo.ticks(["m_szLastPlaceName"], players_only=True).select(
        pl.col("tick").cast(pl.Int64),
        pl.col("steamid"),
        pl.col("m_szLastPlaceName").replace("", None).alias("place"),
    )

    snapshots = demo.snapshots(seconds=SNAPSHOT_SECONDS)
    positions = build_positions(snapshots, places, windows, tick_rate)

    # Utility düşüş noktalarına bölge adı vermek için referans noktalar
    ref = positions.filter(pl.col("place_name").is_not_null())
    ref_points = ref.select("x", "y", "z").to_numpy().astype(np.float64)
    ref_places = ref["place_name"].to_list()

    proj = resolve_projectiles(demo.grenades, demo.shots)

    # Harita çarpışma modeli (ilk kullanımda awpy-data'dan indirilir); yoksa duvar
    # arkası tespiti atlanır, diğer tablolar etkilenmez
    map_name = demo.header.get("map_name", "")
    try:
        vis = VisibilityChecker(map_name) if map_name else None
    except Exception:
        vis = None

    players = demo.players.filter(pl.col("steamid") > 0).select(
        _steamid("steamid").alias("steamid"),
        "name",
        _side("side").alias("side"),
        "team_clan_name",
    )

    return DemoTables(
        map_name=map_name,
        tick_rate=tick_rate,
        players=players,
        rounds=build_rounds(demo.rounds, demo.round_economy, demo.bomb, windows),
        kills=build_kills(demo.kills, places, windows, tick_rate),
        utility=build_utility(demo, proj, windows, ref_points, ref_places, tick_rate),
        positions=positions,
        player_stats=build_player_stats(demo.stats),
        damages=build_damages(demo.damages, places, windows, tick_rate, vis),
    )
