"""Demo parser yardımcılarının testleri (yapay küçük tablolarla, demo dosyası gerekmez)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from director.parse.demo_parser import (
    assign_round,
    build_kills,
    nearest_place,
    resolve_projectiles,
    round_windows,
)

ROUNDS = pl.DataFrame(
    {
        "round_num": [1, 2],
        "start_tick": [None, 1000],
        "freeze_end_tick": [500, 1200],
        "end_tick": [900, 1800],
        "official_end_tick": [1000, 1900],
        "winner": [3, 2],
        "winner_side": ["counter-terrorist", "terrorist"],
        "reason": [8, 9],
        "reason_name": ["ct_win", "terrorists_win"],
        "is_knife_round": [False, False],
    },
    schema_overrides={"start_tick": pl.Int32},
)


def test_assign_round_drops_warmup_and_freeze_time():
    windows = round_windows(ROUNDS)
    events = pl.DataFrame({"tick": [100, 600, 950, 1100, 1300, 2000]})

    out = assign_round(events, windows)

    # 100: ısınma, 1100: round 2 freeze time, 2000: maç sonrası → atılır
    # 950: round 1 bittikten sonra ama resmi bitişten önce (exit frag) → round 1
    assert out["tick"].to_list() == [600, 950, 1300]
    assert out["round_no"].to_list() == [1, 1, 2]


def test_knife_round_is_excluded():
    knife = ROUNDS.with_columns(pl.Series("is_knife_round", [True, False]))
    assert round_windows(knife)["round_no"].to_list() == [2]


def _kills(ticks: list[int]) -> pl.DataFrame:
    n = len(ticks)
    sid = pl.Series([76561198000000001] * n, dtype=pl.UInt64)
    return pl.DataFrame(
        {
            "tick": pl.Series(ticks, dtype=pl.Int32),
            "attacker_steamid": sid,
            "attacker_name": ["a"] * n,
            "attacker_side": ["terrorist"] * n,
            "victim_steamid": pl.Series([76561198000000002] * n, dtype=pl.UInt64),
            "victim_name": ["v"] * n,
            "victim_side": ["counter-terrorist"] * n,
            "assister_steamid": pl.Series([0] * n, dtype=pl.UInt64),
            "weapon": ["ak47"] * n,
            "headshot": [True] * n,
            "assist_flash": [False] * n,
            "thrusmoke": [False] * n,
            **{f"{w}_{c}": [0.0] * n for w in ("attacker", "victim") for c in "xyz"},
            "is_trade": [False] * n,
            "victim_traded": [True] * n,
        }
    )


def test_build_kills_marks_opening_and_time():
    windows = round_windows(ROUNDS)
    places = pl.DataFrame(
        {"tick": [628], "steamid": pl.Series([76561198000000001], dtype=pl.UInt64),
         "place": ["LongA"]}
    )

    k = build_kills(_kills([300, 628, 700, 1264]), places, windows, tick_rate=64.0)

    assert k.height == 3  # 300 ısınma
    assert k["is_opening"].to_list() == [True, False, True]
    assert k["time_in_round"].to_list() == [2.0, 3.125, 1.0]
    assert k["attacker_place"].to_list() == ["LongA", None, None]
    # Steam id metne çevrilir, 0 (assist yok) → null
    assert k["attacker"][0] == "76561198000000001"
    assert k["assister"][0] is None


def test_resolve_projectiles_finds_thrower_from_nearest_throw():
    grenades = pl.DataFrame(
        {
            "tick": [110, 111, 112, 110, 111],
            "thrower_name": [None] * 5,
            "thrower_steamid": pl.Series([None] * 5, dtype=pl.UInt64),
            "thrower_side": [None] * 5,
            "type": ["smoke", "smoke", "smoke", "flashbang", "flashbang"],
            "entity_id": [1, 1, 1, 2, 2],
            "entity_serial": pl.Series([9, 9, 9, 9, 9], dtype=pl.UInt32),
            "x": [100.0, 110, 120, 500, 510], "y": [0.0] * 5, "z": [0.0] * 5,
        }
    )
    # İki oyuncu aynı anda smoke atıyor; mermiye yakın olan atandır
    shots = pl.DataFrame(
        {
            "tick": [105, 106, 108],
            "steamid": pl.Series([11, 22, 33], dtype=pl.UInt64),
            "name": ["yakin", "uzak", "flashci"],
            "side": ["terrorist"] * 3,
            "weapon": ["weapon_smokegrenade", "weapon_smokegrenade", "weapon_flashbang"],
            "x": [95.0, 2000.0, 495.0], "y": [0.0] * 3, "z": [0.0] * 3,
        }
    )

    proj = resolve_projectiles(grenades, shots).sort("entity_id")

    assert proj["thrower_name"].to_list() == ["yakin", "flashci"]
    assert proj["start_tick"].to_list() == [110, 110]
    assert proj["end_tick"].to_list() == [112, 111]


def test_nearest_place_respects_max_distance():
    ref = np.array([[0.0, 0.0, 0.0], [1000.0, 0.0, 0.0]])
    pts = np.array([[50.0, 0.0, 0.0], [990.0, 0.0, 300.0], [5000.0, 0.0, 0.0]])

    out = nearest_place(pts, ref, ["TSpawn", "LongA"], max_distance=600)

    # z farkı yarı ağırlıklı: havadaki flash yine LongA sayılır; çok uzak nokta → None
    assert out == ["TSpawn", "LongA", None]


# --- gerçek demo ile (dosya varsa) -------------------------------------------

REAL_DEMO = os.getenv("DIRECTOR_TEST_DEMO")


@pytest.mark.skipif(not REAL_DEMO or not Path(REAL_DEMO).exists(),
                    reason="DIRECTOR_TEST_DEMO ile açılmış bir .dem yolu verilmedi")
def test_real_demo_matches_awpy_scoreboard():
    from director.parse.demo_parser import parse_demo

    t = parse_demo(REAL_DEMO)

    # Round başına tam bir opening kill
    assert t.kills["is_opening"].sum() == t.rounds.height
    # Isınma kill'leri atıldıktan sonra kill sayıları awpy skor tablosuyla ±1 tutmalı
    ours = t.kills.group_by("attacker").len()
    board = t.player_stats.join(ours, left_on="steam_id", right_on="attacker", how="left")
    assert (board["kills"] - board["len"].fill_null(0)).abs().max() <= 1
    # Utility'lerin neredeyse hepsinde atan kişi bulunmalı
    assert t.utility["thrower"].null_count() <= 0.05 * t.utility.height
