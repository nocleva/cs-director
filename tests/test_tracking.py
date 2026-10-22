"""Öneri takibi testleri: metrik türleri, plan öncesi / sonrası bölme, durum."""

from __future__ import annotations

import json
from datetime import date, datetime

import polars as pl
import pytest

from squadmind.analysis.player_metrics import Rate
from squadmind.analysis.tracking import TrackItem, _status, load_plan, measure, track
from tests.test_team import A, B, E, make_team_data
from tests.test_team_management import _kill


@pytest.fixture
def d():
    d = make_team_data()
    d.kills = pl.DataFrame([
        # R5 (takım CT): E, B'yi 20. sn'de öldürür (trade edildi); A, E'yi
        _kill(5, 20, E, "T", B, "CT", opening=True, traded=True),
        _kill(5, 22, A, "CT", E, "T"),
        # R6: E, A'yı 40. sn'de öldürür (geç ölüm, trade yok)
        _kill(6, 40, E, "T", A, "CT"),
    ]).with_columns(pl.lit("MidDoors").alias("victim_place"))
    return d


def _item(kind, **params):
    return TrackItem(label=kind, rule="-", kind=kind, direction="down", params=params)


def test_measure_kinds(d):
    late = measure(_item("late_deaths_at_place", player=A, side="CT", place="MidDoors",
                         after=30), d, A)
    assert (late.num, late.den) == (1, 2)  # 2 CT round'unda 1 geç ölüm
    traded = measure(_item("traded_at_place", player=B, side="CT", place="MidDoors"), d, A)
    assert (traded.num, traded.den) == (1, 1)
    opening = measure(_item("opening_attempts", player=B, side="CT"), d, A)
    assert (opening.num, opening.den) == (1, 2)
    team = measure(_item("team_traded_death"), d, A)
    assert (team.num, team.den) == (1, 2)  # takımın 2 ölümü (B, A), 1'i trade edildi
    share = measure(_item("spot_share", player=A, side="CT", spot="A"), d, A)
    assert (share.num, share.den) == (2, 2)
    # Bu maçta takım CT'deyken plant yok → retake örneği 0
    assert measure(_item("retake_win"), d, A).den == 0


def test_unknown_player_gives_empty_rate(d):
    r = measure(_item("isolated_death", player="yok_boyle_biri", side="CT"), d, A)
    assert r.value is None


def test_track_splits_by_plan_date(d, tmp_path):
    # İkinci maç plan tarihinden sonra
    d2 = make_team_data()
    def as_m2(df):
        return df.with_columns(pl.lit("m2").alias("match_id"))
    d.rounds = pl.concat([d.rounds, as_m2(d2.rounds)])
    d.roster = pl.concat([d.roster, as_m2(d2.roster)])
    d.positions = pl.concat([d.positions, as_m2(d2.positions)])
    d.match_maps = pl.DataFrame({"match_id": ["m", "m2"], "map": ["de_dust2"] * 2,
                                 "date": [datetime(2026, 10, 1), datetime(2026, 10, 23)]})
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({
        "name": "test", "nickname": A, "map": "de_dust2", "since": "2026-10-22",
        "items": [{"label": "geç ölüm", "rule": "-", "kind": "late_deaths_at_place",
                   "direction": "down", "fmt": "ratio",
                   "params": {"player": A, "side": "CT", "place": "MidDoors", "after": 30}}],
    }), encoding="utf-8")
    plan = load_plan(plan_file)
    assert plan.since == date(2026, 10, 22)

    rows, n_before, n_after = track(d, plan)
    assert (n_before, n_after) == (1, 1)
    assert (rows[0].before.num, rows[0].before.den) == (1, 2)
    assert rows[0].after.den == 2 and rows[0].after.num == 0  # m2'de kill yok
    assert rows[0].status == "veri bekleniyor"  # sonra tarafında 3 maçtan az


def test_status_rules():
    up = TrackItem("x", "-", "team_traded_death", "up")
    assert _status(up, Rate(16, 100), Rate(25, 100), 5) == "iyileşti"
    assert _status(up, Rate(16, 100), Rate(10, 100), 5) == "kötüleşti"
    assert _status(up, Rate(16, 100), Rate(17, 100), 5) == "değişim yok"
    assert _status(up, Rate(16, 100), Rate(25, 100), 2) == "veri bekleniyor"
    down = TrackItem("x", "-", "late_deaths_at_place", "down", fmt="ratio")
    assert _status(down, Rate(12, 100), Rate(6, 100), 4) == "iyileşti"


def test_new_measure_kinds(d):
    # B'nin tek opening düellosu (CT) kaybedildi → 0-1
    rec = measure(_item("opening_record", player=B, side="CT"), d, A)
    assert (rec.num, rec.den) == (0, 1)
    # B'yi öldüren E'yi A 2 sn sonra öldürdü: B'nin 1 ölümünün 1'ini A trade etti
    tb = measure(_item("traded_by", player=B, by=A), d, A)
    assert (tb.num, tb.den) == (1, 1)
    # Flash: sadece o oyuncunun, o taraftaki, o bölgeye düşen flash'ları
    d.utility = pl.DataFrame({
        "thrower": [A, A, A, B], "thrower_side": ["CT", "CT", "T", "CT"],
        "type": ["flash", "flash", "flash", "flash"],
        "land_place": ["LongDoors", "LongDoors", "LongDoors", "LongDoors"],
        "flashed_enemies": [2, None, 5, 3], "damage": [0.0, 0.0, 0.0, 0.0]})
    fl = measure(_item("flash_at_place", player=A, side="CT", place="LongDoors"), d, A)
    assert (fl.num, fl.den) == (2, 2)
