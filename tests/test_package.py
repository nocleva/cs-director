"""WASM demo paketi (`squadmind.parse.package`) testleri.

Sentetik küçük paketlerle `PackageDemo` dtype / sütun / sürüm davranışı; gerçek demo,
Node ve derlenmiş WASM varsa paket ile awpy yolunun tablo karşılaştırması (yoksa atlanır).
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pytest

from squadmind.parse import package as pkg
from squadmind.parse.demo_parser import DemoTables, parse_demo, tables_from_demo
from squadmind.parse.package import PackageDemo, PackageError, parse_package

ROOT = Path(__file__).resolve().parents[1]

# Gerçek awpy 3.1.1 `Demo` çıktısından alınan sütun → dtype sözleşmesi (tests/fixtures/awpy_schema.json;
# `Demo.<tablo>.schema`, snapshots(seconds=2), ticks(["m_szLastPlaceName"]), grenades, shots)
CONTRACT: dict[str, dict[str, pl.DataType]] = {
    table: {col: getattr(pl, dtype) for col, dtype in cols.items()}
    for table, cols in json.loads(
        (Path(__file__).parent / "fixtures" / "awpy_schema.json").read_text(encoding="utf-8")
    ).items()
}


def _col(name: str, dtype: str, values: list) -> dict:
    return {"name": name, "dtype": dtype, "values": values}


def _table(*cols: dict) -> dict:
    return {"columns": list(cols)}


def _empty(spec: dict[str, pl.DataType]) -> dict:
    """Sözleşmeden boş (0 satırlı) paket tablosu."""
    rev = {v: k for k, v in pkg.DTYPES.items()}
    return _table(*[_col(n, rev[t], []) for n, t in spec.items()])


def make_package(**overrides) -> dict:
    """Her tablosu boş ama dtype'ları doğru, geçerli bir sentetik paket."""
    base = {
        "format_version": pkg.FORMAT_VERSION,
        "awpy_version": "3.1.1",
        "producer": "test",
        "tick_rate": 64.0,
        "snapshot_seconds": 2.0,
        "header": {"map_name": "de_dust2"},
        "reductions": [],
        "events": {},
    }
    for name in (*pkg.TABLE_NAMES, "snapshots", "places"):
        base[name] = _empty(CONTRACT[name])
    base.update(overrides)
    return base


# --- PackageDemo ---------------------------------------------------------------

def test_dtypes_follow_contract_even_when_empty():
    demo = PackageDemo(make_package())
    for table, spec in CONTRACT.items():
        frame = (demo.snapshots(seconds=2.0) if table == "snapshots"
                 else demo.ticks(["m_szLastPlaceName"]) if table == "places"
                 else getattr(demo, table))
        assert frame.height == 0
        assert dict(frame.schema) == spec, table


def test_values_nulls_and_big_steamids():
    big = 76561198000000001  # JS sayı hassasiyetini aşar; Python int olarak korunur
    players = _table(
        _col("steamid", "u64", [big, None]),
        _col("name", "str", ["a", None]),
        _col("side", "str", ["terrorist", None]),
        _col("team_clan_name", "str", [None, "x"]),
    )
    demo = PackageDemo(make_package(players=players))
    assert demo.players["steamid"].to_list() == [big, None]
    assert demo.players.schema["steamid"] == pl.UInt64
    assert demo.players["team_clan_name"].to_list() == [None, "x"]


def test_float_specials_roundtrip():
    snap = _table(_col("pitch", "f32", [1.5, "nan", "inf", "-inf", None]))
    demo = PackageDemo(make_package(snapshots=snap))
    col = demo.snapshots(seconds=2.0)["pitch"]
    assert col.dtype == pl.Float32
    values = col.to_list()
    assert values[0] == 1.5 and values[1] != values[1]  # NaN
    assert values[2] == float("inf") and values[3] == float("-inf") and values[4] is None


def test_tick_rate_is_python_float_and_header_dict():
    demo = PackageDemo(make_package(tick_rate=63.99999237060547))
    assert isinstance(demo.tick_rate, float) and demo.tick_rate == 63.99999237060547
    assert demo.header.get("map_name") == "de_dust2"


def test_events_mapping_behaves_like_awpy_events():
    ev = _table(_col("tick", "i64", [10]), _col("entityid", "str", ["7"]), _col("x", "str", ["1.5"]))
    demo = PackageDemo(make_package(events={"flashbang_detonate": ev}))
    assert "flashbang_detonate" in demo.events and "hegrenade_detonate" not in demo.events
    assert demo.events["flashbang_detonate"].schema["tick"] == pl.Int64
    assert demo.events["flashbang_detonate"]["x"].dtype == pl.String
    assert demo.events.names == ["flashbang_detonate"]


def test_only_packaged_queries_are_supported():
    demo = PackageDemo(make_package())
    assert demo.ticks(["m_szLastPlaceName"], players_only=True).columns == [
        "tick", "steamid", "m_szLastPlaceName"]
    with pytest.raises(NotImplementedError):
        demo.ticks(["X"], players_only=True)
    with pytest.raises(NotImplementedError):
        demo.snapshots(seconds=1.0)


def test_unknown_format_version_and_dtype_are_rejected():
    with pytest.raises(PackageError):
        PackageDemo(make_package(format_version=99))
    bad = make_package(rounds=_table(_col("round_num", "i128", [])))
    with pytest.raises(PackageError):
        PackageDemo(bad)


def test_load_package_accepts_gzip_plain_and_path(tmp_path: Path):
    raw = json.dumps(make_package()).encode()
    assert pkg.load_package(gzip.compress(raw))["format_version"] == pkg.FORMAT_VERSION
    assert pkg.load_package(raw)["awpy_version"] == "3.1.1"
    f = tmp_path / "p.json.gz"
    f.write_bytes(gzip.compress(raw))
    assert pkg.package_demo(f).tick_rate == 64.0
    with pytest.raises(PackageError):
        pkg.load_package(b"bu bir paket degil")


def test_tables_from_demo_runs_on_empty_package():
    """Boş (ama geçerli) paketle de tüm tablo kurucuları çalışır, boş tablolar döner."""
    rounds = _table(
        _col("round_num", "i32", [1]), _col("start_tick", "i32", [None]),
        _col("freeze_end_tick", "i32", [100]), _col("end_tick", "i32", [900]),
        _col("official_end_tick", "i32", [1000]), _col("winner", "i32", [3]),
        _col("winner_side", "str", ["counter-terrorist"]), _col("reason", "i32", [8]),
        _col("reason_name", "str", ["ct_win"]), _col("is_knife_round", "bool", [False]),
    )
    tables = tables_from_demo(PackageDemo(make_package(rounds=rounds)))
    assert isinstance(tables, DemoTables)
    assert tables.map_name == "de_dust2" and tables.tick_rate == 64.0
    assert tables.rounds["round_no"].to_list() == [1]
    assert tables.kills.height == 0 and tables.positions.height == 0


# --- gerçek demo ile karşılaştırma ---------------------------------------------

DEMO_DIR = Path.home() / "squadmind-data" / "demos" / "processed"
WASM_PKG = ROOT / "wasm" / "awpy-wasm" / "pkg-web" / "awpy_wasm_bg.wasm"


@pytest.mark.skipif(
    shutil.which("node") is None or not WASM_PKG.exists() or not DEMO_DIR.exists()
    or not list(DEMO_DIR.glob("*.dem.zst")),
    reason="Node, derlenmiş WASM (wasm/awpy-wasm/pkg-web) ve gerçek demo gerekir",
)
def test_package_matches_awpy_on_real_demo(tmp_path: Path):
    """Bir gerçek demoda paket yolu ile awpy yolu her tabloda birebir aynı sonucu verir."""
    import zstandard

    src = sorted(DEMO_DIR.glob("*.dem.zst"))[0]
    dem = tmp_path / "demo.dem"
    with src.open("rb") as fi, dem.open("wb") as fo:
        zstandard.ZstdDecompressor().copy_stream(fi, fo)
    out = tmp_path / "demo.pkg.json.gz"
    done = subprocess.run(
        ["node", str(ROOT / "wasm" / "awpy-wasm" / "node_pack.js"), str(dem), str(out)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr

    ref, got = parse_demo(dem), parse_package(out)
    assert ref.map_name == got.map_name and ref.tick_rate == got.tick_rate
    for name in ("players", "rounds", "kills", "utility", "positions", "player_stats", "damages"):
        a, b = getattr(ref, name), getattr(got, name)
        assert a.schema == b.schema, name
        assert a.equals(b), name
