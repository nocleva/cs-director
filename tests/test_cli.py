"""CLI testleri: init-db ve summary komutları (API'ye gitmeden)."""

from __future__ import annotations

from typer.testing import CliRunner

from director.cli import app
from director.db import connect, init_db
from director.ingest.matches import sync_player_matches

runner = CliRunner()


def test_init_db_creates_all_tables(tmp_path):
    db = tmp_path / "t.duckdb"
    result = runner.invoke(app, ["init-db", "--db", str(db)])

    assert result.exit_code == 0, result.output
    for table in ("players", "matches", "match_player_stats", "rounds", "kills",
                  "utility", "positions_snapshot"):
        assert table in result.output


def test_summary_shows_own_score_first(tmp_path, make_client):
    db = tmp_path / "t.duckdb"
    with connect(db) as con:
        init_db(con)
        sync_player_matches(make_client(), con, "TestNick")

    result = runner.invoke(app, ["summary", "TestNick", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "mirage" in result.output and "inferno" in result.output
    assert "13-9" in result.output   # faction1 olarak kazandığı maç
    assert "11-13" in result.output  # faction2 olarak kaybettiği maç: kendi skoru önce


def test_summary_without_db_fails_cleanly(tmp_path):
    result = runner.invoke(app, ["summary", "TestNick", "--db", str(tmp_path / "yok.duckdb")])
    assert result.exit_code == 1
