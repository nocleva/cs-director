"""Maç ingest testleri: ayrıştırma, upsert, demo durumu."""

from __future__ import annotations

from datetime import datetime

from conftest import load_fixture

from director.ingest.matches import parse_match, sync_player_matches


def test_parse_match_basic_fields():
    parsed = parse_match(load_fixture("match_m-1.json"), load_fixture("stats_m-1.json"))

    m = parsed.match
    assert m["match_id"] == "m-1"
    assert m["map"] == "de_mirage"
    assert (m["score_a"], m["score_b"], m["score"]) == (13, 9, "13-9")
    assert m["team_a"] == "team_TestNick"
    assert m["demo_url"].endswith("m-1.dem.gz")
    assert m["demo_status"] == "pending"
    assert m["date"] == datetime(2025, 10, 18, 15, 6, 40)

    me = next(r for r in parsed.player_stats if r["player_id"] == "p-self")
    assert me["kills"] == 24 and me["deaths"] == 15
    assert me["adr"] == 98.4 and me["hs_pct"] == 54.0
    assert me["faction"] == "faction1" and me["result"] is True

    steam = {p["faceit_id"]: p["steam_id"] for p in parsed.players}
    assert steam["p-enemy1"] == "76561198000000003"


def test_parse_match_faction_fallback_without_team_ids():
    # m-2'de stats team_id'leri detaydaki faction_id ile eşleşmiyor
    parsed = parse_match(load_fixture("match_m-2.json"), load_fixture("stats_m-2.json"))

    me = next(r for r in parsed.player_stats if r["player_id"] == "p-self")
    assert me["faction"] == "faction2"
    assert me["result"] is False
    assert parsed.match["demo_status"] == "missing"


def test_sync_writes_matches_and_stats(make_client, con):
    result = sync_player_matches(make_client(), con, "TestNick", limit=30)

    assert result.matches_seen == 3
    assert result.matches_written == 2
    assert [s[0] for s in result.skipped] == ["m-0"]  # iptal edilen maç

    assert con.execute("SELECT count(*) FROM matches").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM match_player_stats").fetchone()[0] == 6

    elo, level, steam = con.execute(
        "SELECT elo, level, steam_id FROM players WHERE faceit_id = 'p-self'"
    ).fetchone()
    assert (elo, level, steam) == (1789, 8, "76561198000000001")


def test_resync_does_not_duplicate(make_client, con):
    client = make_client()
    sync_player_matches(client, con, "TestNick")
    sync_player_matches(client, con, "TestNick")

    assert con.execute("SELECT count(*) FROM matches").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM match_player_stats").fetchone()[0] == 6
    # p-self, p-mate, p-enemy1, p-enemy2, p-other1
    assert con.execute("SELECT count(*) FROM players").fetchone()[0] == 5


def test_resync_keeps_processed_demo_status_and_demo_kast(make_client, con):
    client = make_client()
    sync_player_matches(client, con, "TestNick")
    # Faz 1b sonrası durumu taklit et: demo işlendi, KAST demodan yazıldı
    con.execute("UPDATE matches SET demo_status = 'processed' WHERE match_id = 'm-1'")
    con.execute(
        "UPDATE match_player_stats SET kast = 75.0 WHERE match_id = 'm-1' AND player_id = 'p-self'"
    )

    sync_player_matches(client, con, "TestNick")

    assert con.execute(
        "SELECT demo_status FROM matches WHERE match_id = 'm-1'"
    ).fetchone()[0] == "processed"
    assert con.execute(
        "SELECT kast FROM match_player_stats WHERE match_id = 'm-1' AND player_id = 'p-self'"
    ).fetchone()[0] == 75.0


def test_sync_skips_match_without_stats(make_client, con):
    import httpx

    from conftest import fake_faceit_handler

    def no_stats_for_m2(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/matches/m-2/stats"):
            return httpx.Response(404)
        return fake_faceit_handler(request)

    result = sync_player_matches(make_client(no_stats_for_m2), con, "TestNick")
    assert result.matches_written == 1
    assert ("m-2", "istatistik bulunamadı (404)") in result.skipped
