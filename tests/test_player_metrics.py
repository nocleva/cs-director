"""Oyuncu metrikleri, rol sınıflandırma ve oyuncu raporu testleri.

Küçük, elle hesaplanabilir bir maç kurulur: 2'ye 2, 4 round, Dust2.
Takım 1 (faction1): A, B · Takım 2 (faction2): C, D
Round 1-2: takım 1 CT · Round 3-4: takım 2 CT
"""

from __future__ import annotations

import polars as pl
import pytest

from director.analysis.player_metrics import (
    DemoData,
    Rate,
    aggregate,
    ct_zone_behaviour,
    economy_breakdown,
    lurk_distance,
    player_match_metrics,
    player_rounds,
    weapon_breakdown,
)
from director.analysis.roles import primary_roles, role_scores, side_roles, team_signals
from director.reports.player_report import highlights

A, B, C, D = "1", "2", "3", "4"
M = "m"


def _kill(rnd, t, att, att_side, vic, vic_side, weapon="ak47", opening=False,
          traded=False, trade_kill=False, hs=False, att_place="LongA", vic_place="LongDoors"):
    return {
        "match_id": M, "round_no": rnd, "tick": int(t * 64), "time_in_round": float(t),
        "attacker": att, "attacker_name": att, "attacker_side": att_side,
        "victim": vic, "victim_name": vic, "victim_side": vic_side,
        "weapon": weapon, "headshot": hs, "is_opening": opening,
        "is_trade_kill": trade_kill, "is_traded": traded,
        "attacker_place": att_place, "victim_place": vic_place,
    }


def make_data() -> DemoData:
    rounds = pl.DataFrame({
        "match_id": [M] * 4, "round_no": [1, 2, 3, 4],
        "winner_side": ["CT", "T", "CT", "CT"],
        "ct_faction": ["faction1", "faction1", "faction2", "faction2"],
        "ct_buy_type": ["pistol", "full", "full", "eco"],
        "t_buy_type": ["pistol", "force", "full", "full"],
    })
    roster = pl.DataFrame({
        "match_id": [M] * 4, "steam_id": [A, B, C, D], "faceit_id": ["fa", "fb", "fc", "fd"],
        "nickname": ["A", "B", "C", "D"], "faction": ["faction1"] * 2 + ["faction2"] * 2,
    })
    kills = pl.DataFrame([
        # R1: takım 1 CT. A (CT) opening'i AWP ile kazanır; C (T) öldürür B'yi, D trade'ler
        _kill(1, 10, A, "CT", C, "T", "awp", opening=True),
        _kill(1, 20, D, "T", B, "CT", traded=True),
        _kill(1, 22, A, "CT", D, "T", "ssg08", trade_kill=True),
        # R2: C (T) opening'de A'yı öldürür; A'nın ölümü trade edilmez
        _kill(2, 30, C, "T", A, "CT", opening=True, vic_place="MidDoors"),
        # R3: takım 1 T. A opening'i kazanır (T)
        _kill(3, 8, A, "T", C, "CT", "awp", opening=True, hs=True),
        # R4: B takım arkadaşı A'yı vurur (sayılmaz kill), sonra D B'yi öldürür
        _kill(4, 40, B, "T", A, "T"),
        _kill(4, 70, D, "CT", B, "T", opening=False),
    ])
    utility = pl.DataFrame({
        "match_id": [M] * 4, "round_no": [1, 2, 3, 4], "thrower": [B, B, B, A],
        "type": ["flash", "flash", "smoke", "he"],
        "flashed_enemies": [2, 1, None, None], "flashed_teammates": [1, 0, None, None],
        "blind_duration": [3.0, 1.0, None, None], "damage": [None, None, None, 40.0],
    })
    # T konumları (round 3-4: A ve B T) — A takımdan uzakta, B yanında
    # CT konumları: A round 1'de B site → A site'a döner; B round 1-2 hep A
    positions = pl.DataFrame({
        "match_id": [M] * 10,
        "round_no": [3, 3, 3, 1, 1, 1, 2, 2, 1, 2],
        "tick": [600, 600, 600, 1500, 2000, 4000, 1500, 1500, 1500, 1500],
        "time_in_round": [10.0, 10.0, 10.0, 20.0, 30.0, 60.0, 20.0, 20.0, 20.0, 20.0],
        "player": [A, B, C, A, A, A, A, B, B, D],
        "side": ["T", "T", "T", "CT", "CT", "CT", "CT", "CT", "CT", "T"],
        "x": [1000.0, 0.0, 0.0, 0, 0, 0, 0, 0, 0, 0], "y": [0.0] * 10,
        "place_name": ["LongA", "Middle", "Middle", "BombsiteB", "BombsiteB", "LongA",
                       "BombsiteB", "LongA", "LongA", "Middle"],
    })
    stats = pl.DataFrame({
        "match_id": [M] * 4, "steam_id": [A, B, C, D], "rounds_played": [4] * 4,
        "kast": [75.0, 50.0, 50.0, 75.0], "adr": [100.0, 40.0, 60.0, 70.0],
        "flash_assists": [0, 1, 0, 0], "clutches_played": [1, 0, 0, 0],
        "clutches_won": [1, 0, 0, 0], "multikill_3k": [0] * 4, "multikill_4k": [0] * 4,
        "multikill_5k": [0] * 4,
    })
    maps = pl.DataFrame({"match_id": [M], "map": ["de_dust2"], "date": [None]})
    return DemoData(rounds, roster, kills, utility, positions, stats, maps)


@pytest.fixture
def d() -> DemoData:
    return make_data()


@pytest.fixture
def pm(d) -> pl.DataFrame:
    return player_match_metrics(d)


def row(pm: pl.DataFrame, sid: str) -> dict:
    return pm.filter(pl.col("steam_id") == sid).row(0, named=True)


def test_player_rounds_side_win_and_pistol(d):
    pr = player_rounds(d).filter(pl.col("steam_id") == A).sort("round_no")
    assert pr["side"].to_list() == ["CT", "CT", "T", "T"]
    assert pr["won"].to_list() == [True, False, False, False]
    assert pr["own_buy"].to_list() == ["pistol", "full", "full", "full"]
    assert pr["is_pistol"].to_list() == [True, False, False, False]


def test_counts_per_side_and_teamkill_excluded(pm):
    a = row(pm, A)
    assert (a["kills"], a["t_kills"], a["ct_kills"]) == (3, 1, 2)
    assert (a["t_ok"], a["ct_ok"], a["ct_od"], a["t_od"]) == (1, 1, 1, 0)
    # A iki kez ölür: R2 (C) ve R4 (takım arkadaşı B) — ölümler hepsi sayılır
    assert a["deaths"] == 2
    assert (a["k_awp"], a["k_scout"]) == (2, 1)
    b = row(pm, B)
    assert b["kills"] == 0  # takım arkadaşı öldürme kill sayılmaz
    assert b["traded_deaths"] == 1


def test_aggregate_rates(pm):
    r = aggregate(pm.filter(pl.col("steam_id") == A))
    assert r["kd"].value == 1.5
    assert r["ct_open_win"].pct() == "%50 (1/2)"
    assert r["t_open_attempt"].pct() == "%50 (1/2)"
    assert r["sniper_share"].value == 1.0
    assert r["adr"].value == 100.0
    assert r["kast"].value == 0.75
    assert r["clutch_win"].pct() == "%100 (1/1)"
    assert r["pistol_win"].pct() == "%100 (1/1)"


def test_rate_handles_zero_denominator():
    assert Rate(3, 0).value is None and Rate(3, 0).pct() == "-"


def test_lurk_distance_is_distance_to_teammates_centroid(d):
    lurk = lurk_distance(d).sort("steam_id")
    # A x=1000, diğerleri x=0 → merkez 0 → A'nın uzaklığı 1000; B: (1000+0)/2=500
    assert lurk.filter(pl.col("steam_id") == A)["lurk_dist"][0] == 1000
    assert lurk.filter(pl.col("steam_id") == B)["lurk_dist"][0] == 500


def test_ct_zone_anchor_and_rotation(d):
    z = ct_zone_behaviour(d)
    a = z.filter(pl.col("steam_id") == A).row(0, named=True)
    b = z.filter(pl.col("steam_id") == B).row(0, named=True)
    # A: R1 B'de kurulur sonra A'ya geçer (rotasyon); R2 B'de → ev alanı B
    assert (a["ct_setup_rounds"], a["ct_rotations"], a["ct_home_zone"]) == (2, 1, "B")
    # B: iki round da A'da → anchor
    assert (b["ct_anchor_rounds"], b["ct_rotations"], b["ct_home_zone"]) == (2, 0, "A")


def test_roles_awper_anchor_and_support(pm):
    a = role_scores(pm, A)
    assert a["awper"].confidence == "düşük"  # sinyal net ama tek maç
    b = role_scores(pm, B)
    # Support 3 sinyalin ortalaması: flash ve kör etmede takımında 1., utility hasarında
    # (A'nın HE'si) 2. → (1 + 1 + 0) / 3
    assert b["support"].score == pytest.approx(2 / 3)
    # A da B de iki round aynı site'ta kuruldu → eşit, takım içi yüzdelik 0,5
    assert b["anchor"].score == 0.5
    t_role, ct_role = primary_roles(a)
    assert t_role.role == "awper" and ct_role.role == "awper"
    # AWPer varsa birincil, diğer roller ikincil sırada
    assert side_roles(a)["T"][0].role == "awper"


def test_team_signals_are_within_team(pm):
    sig = team_signals(pm)
    # 2 kişilik takımda yüzdelik 0 ya da 1, eşitlikte 0,5 olabilir
    vals = sig["support"].drop_nulls().to_list() + sig["anchor"].drop_nulls().to_list()
    allowed = (0.0, 1 / 3, 0.5, 2 / 3, 1.0)
    assert all(any(abs(v - a) < 1e-9 for a in allowed) for v in vals)


def test_weapon_and_economy_breakdown(d):
    w = weapon_breakdown(d, A)
    assert w.filter(pl.col("weapon") == "awp")["kills"][0] == 2
    eco = economy_breakdown(d, A)
    ct_pistol = eco.filter((pl.col("side") == "CT") & (pl.col("own_buy") == "pistol"))
    assert ct_pistol.select("rounds", "wins", "kills").row(0) == (1, 1, 2)


def test_highlights_respect_sample_and_point_thresholds():
    me = {"kd": Rate(30, 20), "round_win": Rate(52, 100), "t_open_win": Rate(9, 12)}
    lobby = {"kd": Rate(100, 100), "round_win": Rate(50, 100), "t_open_win": Rate(50, 100)}
    full_me = {k: me.get(k, Rate(0, 0)) for k in _all_keys()}
    full_lb = {k: lobby.get(k, Rate(0, 0)) for k in _all_keys()}
    good, bad = highlights(full_me, full_lb)
    # K/D +%50 → belirgin; round_win +2 puan → değil; t_open_win 12 örnek → az örneklem
    assert len(good) == 1 and "K/D" in good[0] and bad == []


def _all_keys() -> list[str]:
    from director.reports.player_report import SECTIONS

    return [m.key for ms in SECTIONS.values() for m in ms]


# --- rapor (DB üzerinden) ----------------------------------------------------

def test_player_report_end_to_end(make_client, con):
    from director.ingest.matches import sync_player_matches
    from director.reports.player_report import build_player_report

    sync_player_matches(make_client(), con, "TestNick")
    data = make_data()
    # Sentetik maçı fixture'daki m-1 maçına ve oyuncularına bağla
    sid = {A: "76561198000000001", B: "76561198000000002",
           C: "76561198000000003", D: "76561198000000004"}

    def remap(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
        df = df.with_columns(pl.lit("m-1").alias("match_id"))
        return df.with_columns([pl.col(c).replace(sid) for c in cols if c in df.columns])

    tables = {
        "rounds": remap(data.rounds, []),
        "kills": remap(data.kills, ["attacker", "victim"]),
        "utility": remap(data.utility, ["thrower"]),
        "positions_snapshot": remap(data.positions, ["player"]),
        "demo_player_stats": remap(data.stats, ["steam_id"]),
    }
    for name, df in tables.items():
        cols = ", ".join(df.columns)
        con.register("_f", df)
        con.execute(f"INSERT INTO {name} ({cols}) SELECT {cols} FROM _f")
        con.unregister("_f")
    con.execute("UPDATE matches SET demo_status = 'processed', map = 'de_dust2' "
                "WHERE match_id = 'm-1'")

    rep = build_player_report(con, "testnick")

    assert rep.demo_matches == 1
    md = rep.markdown
    for heading in ("## Özet", "## Demo metrikleri", "## Roller", "## Silahlar",
                    "## Bölgeler", "## Ekonomi", "## FACEIT geneli"):
        assert heading in md
    assert "AWPer" in md


def test_player_report_unknown_player(con):
    from director.reports.player_report import build_player_report

    with pytest.raises(LookupError):
        build_player_report(con, "kimse")


def test_weapon_place_breakdown_with_damage_and_wallbang(d):
    from director.analysis.player_metrics import weapon_place_breakdown

    d.kills = d.kills.with_columns(
        pl.Series("penetrated", [1, 0, 0, 0, 0, 0, 0], dtype=pl.Int32)
    )
    d.damages = pl.DataFrame({
        "match_id": [M] * 3, "attacker": [A, A, A], "attacker_side": ["CT", "CT", "CT"],
        "victim_side": ["T"] * 3, "weapon": ["awp", "awp", "ssg08"],
        "dmg_health": [100, 40, 75], "dmg": [100, 40, 75], "attacker_place": ["LongA"] * 3,
        "through_wall": [True, False, None],
    })

    wp = weapon_place_breakdown(d, A)
    awp = wp.filter((pl.col("place") == "LongA") & (pl.col("weapon") == "awp")).row(0, named=True)
    # R1 opening AWP kill (wallbang) + R3 AWP kill → 2 kill, 2 opening, 1 wallbang
    assert (awp["kills"], awp["openings"], awp["wallbangs"]) == (2, 2, 1)
    assert (awp["damage"], awp["hits"], awp["wall_damage"]) == (140, 2, 100)
    scout = wp.filter(pl.col("weapon") == "scout").row(0, named=True)
    assert (scout["kills"], scout["damage"]) == (1, 75)


def test_cap_damage_clips_to_remaining_health():
    from director.parse.damage import cap_damage

    dm = pl.DataFrame({
        "match_id": [M] * 5, "round_no": [1, 1, 1, 1, 2], "tick": [10, 20, 30, 40, 10],
        "victim": [C, C, C, D, C],
        # C round 1: 30 (takım arkadaşından da olsa can düşer) + 448 AWP (70 kalmıştı)
        # + ölüdeki ek isabet → 30, 70, 0 · D: 27 · C round 2: can yeniden 100
        "dmg_health": [30, 448, 15, 27, 120],
    })
    out = cap_damage(dm, ["match_id", "round_no", "victim"]).sort("victim", "round_no", "tick")
    assert out["dmg"].to_list() == [30, 70, 0, 100, 27]


def _with_weapons(d: DemoData, weapons: list[str]) -> DemoData:
    d.positions = d.positions.with_columns(pl.Series("active_weapon", weapons))
    return d


def test_round_main_weapon_prefers_most_held_primary(d):
    from director.analysis.player_metrics import round_main_weapon

    # A'nın kayıtları: R3 (1 kayıt), R1 (3 kayıt), R2 (1 kayıt)
    d = _with_weapons(d, ["knife", "ak47", "ak47", "awp", "awp", "knife", "ssg08",
                          "m4a1", "m4a1", "ak47"])
    mw = round_main_weapon(d, A).sort("round_no")
    # R1: awp 2 kez (bıçak sayılmaz) · R2: scout · R3: sadece bıçak → pistol
    assert mw["main_weapon"].to_list() == ["awp", "scout", "pistol"]
    assert mw["held_snaps"].to_list() == [2, 1, 0]


def test_round_main_weapon_without_weapon_column(d):
    from director.analysis.player_metrics import round_main_weapon

    assert round_main_weapon(d, A).height == 0


def test_weapon_exposure_per_round_and_place(d):
    from director.analysis.player_metrics import weapon_exposure

    d = _with_weapons(d, ["knife", "ak47", "ak47", "awp", "awp", "knife", "ssg08",
                          "m4a1", "m4a1", "ak47"])
    d.damages = pl.DataFrame({
        "match_id": [M] * 2, "round_no": [1, 1], "attacker": [A, A],
        "attacker_side": ["CT", "CT"], "victim_side": ["T", "T"],
        "dmg": [100, 60], "through_wall": [True, False],
        "attacker_place": ["LongA", "LongA"],
    })
    ex = weapon_exposure(d, A).sort("side", "main_weapon")
    ct_awp = ex.filter((pl.col("side") == "CT") & (pl.col("main_weapon") == "awp")).row(0, named=True)
    # R1 (CT, kazanıldı): AWP opening + scout ile trade kill → 2 kill, 1'i ana silahla
    assert (ct_awp["rounds"], ct_awp["wins"], ct_awp["kills"], ct_awp["kills_main"]) == (1, 1, 2, 1)
    assert (ct_awp["openings"], ct_awp["damage"], ct_awp["wall_damage"]) == (1, 160, 100)
    assert ct_awp["full_rounds"] == 0  # R1 pistol round
    ct_scout = ex.filter((pl.col("side") == "CT") & (pl.col("main_weapon") == "scout")).row(0, named=True)
    # R2: kayıp round, MidDoors'ta ölüm
    assert (ct_scout["rounds"], ct_scout["wins"], ct_scout["deaths"]) == (1, 0, 1)

    # MidDoors: sadece orada ölünen R2 kalır
    mid = weapon_exposure(d, A, "MidDoors")
    assert mid.select("main_weapon", "rounds", "deaths", "kills").rows() == [("scout", 1, 1, 0)]
    # full buy filtresi: R1 (pistol round) elenir; R2 CT full (scout), R3 T full (sadece bıçak)
    full = weapon_exposure(d, A, buy="full")
    assert full.select("side", "main_weapon").sort("side").rows() == [("CT", "scout"), ("T", "pistol")]
