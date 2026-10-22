"""FACEIT maç geçmişini ve skor tablolarını DuckDB'ye yazar (Faz 1a).

NOT: Alan adları FACEIT Data API v4 dokümantasyonuna göre yazıldı. İlk gerçek
yanıt data/cache/ altına düştükten sonra kontrol edilip gerekirse düzeltilmeli.
Ayrıştırıcılar eksik alanlarda çökmez, None yazar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import duckdb

from director.faceit.client import FaceitClient, FaceitNotFound

FACTIONS = ("faction1", "faction2")


# --- küçük dönüştürücüler ----------------------------------------------------

def _to_float(value: Any) -> float | None:
    """FACEIT istatistikleri string gelir ("85.3"); güvenli float dönüşümü."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(round(f)) if f is not None else None


def _to_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "win", "yes")


def _to_datetime(value: Any) -> datetime | None:
    """Unix saniyesini (FACEIT started_at) UTC naive datetime'a çevirir."""
    ts = _to_float(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


# --- ayrıştırma --------------------------------------------------------------

@dataclass
class ParsedMatch:
    """Bir maçtan çıkan, DB'ye yazılmaya hazır satırlar."""

    match: dict[str, Any]
    player_stats: list[dict[str, Any]]
    players: list[dict[str, Any]]


def _map_name(details: dict[str, Any], round_stats: dict[str, Any]) -> str | None:
    # Önce istatistikteki harita adı ("de_mirage"), yoksa veto sonucundaki pick
    if round_stats.get("Map"):
        return round_stats["Map"]
    picks = ((details.get("voting") or {}).get("map") or {}).get("pick") or []
    return picks[0] if picks else None


def parse_match(details: dict[str, Any], stats: dict[str, Any]) -> ParsedMatch:
    """Maç detayı + istatistik yanıtını tablo satırlarına dönüştürür."""
    match_id = details.get("match_id") or stats.get("match_id")
    teams = details.get("teams") or {}

    # Kadrodan oyuncu → faction eşlemesi ve oyuncu satırları
    player_faction: dict[str, str] = {}
    players: list[dict[str, Any]] = []
    for faction in FACTIONS:
        for member in (teams.get(faction) or {}).get("roster") or []:
            pid = member.get("player_id")
            if not pid:
                continue
            player_faction[pid] = faction
            players.append(
                {
                    "faceit_id": pid,
                    # CS2'de game_player_id = SteamID64 (demo eşleştirmesi için)
                    "steam_id": member.get("game_player_id"),
                    "nickname": member.get("nickname"),
                    "elo": None,  # kadroda ELO yok; sadece profil çağrısından gelir
                    "level": _to_int(member.get("game_skill_level")),
                }
            )

    # BO1 matchmaking'de tek harita olur; BO3'te ilk haritayı alıyoruz
    rounds = stats.get("rounds") or [{}]
    first_map = rounds[0] if rounds else {}
    round_stats = first_map.get("round_stats") or {}

    # Skor: önce detaydaki results, yoksa istatistikteki takım "Final Score"
    results = details.get("results") or {}
    score_map = results.get("score") or {}
    score_a = _to_int(score_map.get("faction1"))
    score_b = _to_int(score_map.get("faction2"))
    stat_teams = first_map.get("teams") or []

    if score_a is None or score_b is None:
        for idx, team in enumerate(stat_teams):
            faction = _faction_for_stat_team(team, idx, teams, player_faction)
            final = _to_int((team.get("team_stats") or {}).get("Final Score"))
            if faction == "faction1":
                score_a = final
            elif faction == "faction2":
                score_b = final

    demo_urls = details.get("demo_url") or []
    if isinstance(demo_urls, str):
        demo_urls = [demo_urls]
    demo_url = demo_urls[0] if demo_urls else None

    match_row = {
        "match_id": match_id,
        "map": _map_name(details, round_stats),
        "date": _to_datetime(details.get("started_at")),
        "team_a": (teams.get("faction1") or {}).get("name"),
        "team_b": (teams.get("faction2") or {}).get("name"),
        "score": f"{score_a}-{score_b}" if score_a is not None and score_b is not None else None,
        "score_a": score_a,
        "score_b": score_b,
        "winner": results.get("winner"),
        "competition": details.get("competition_name"),
        "demo_url": demo_url,
        # Demo linki varsa indirilmeyi bekliyor, yoksa 'missing'
        "demo_status": "pending" if demo_url else "missing",
    }

    # Oyuncu başı skor tablosu
    stat_rows: list[dict[str, Any]] = []
    for idx, team in enumerate(stat_teams):
        team_faction = _faction_for_stat_team(team, idx, teams, player_faction)
        team_name = (team.get("team_stats") or {}).get("Team") or (
            (teams.get(team_faction) or {}).get("name") if team_faction else None
        )
        for p in team.get("players") or []:
            s = p.get("player_stats") or {}
            pid = p.get("player_id")
            stat_rows.append(
                {
                    "match_id": match_id,
                    "player_id": pid,
                    "nickname": p.get("nickname"),
                    "faction": player_faction.get(pid, team_faction),
                    "team_name": team_name,
                    "kills": _to_int(s.get("Kills")),
                    "deaths": _to_int(s.get("Deaths")),
                    "assists": _to_int(s.get("Assists")),
                    "kd_ratio": _to_float(s.get("K/D Ratio")),
                    "adr": _to_float(s.get("ADR")),
                    "hs_pct": _to_float(s.get("Headshots %")),
                    "mvps": _to_int(s.get("MVPs")),
                    "triple_kills": _to_int(s.get("Triple Kills")),
                    "quadro_kills": _to_int(s.get("Quadro Kills")),
                    "penta_kills": _to_int(s.get("Penta Kills")),
                    "result": _to_bool(s.get("Result")),
                    "kast": None,
                    "rating": None,
                }
            )

    return ParsedMatch(match=match_row, player_stats=stat_rows, players=players)


def _faction_for_stat_team(
    team: dict[str, Any],
    idx: int,
    detail_teams: dict[str, Any],
    player_faction: dict[str, str],
) -> str | None:
    """İstatistikteki takımın faction1 mi faction2 mi olduğunu bulur."""
    # 1) team_id, detaydaki faction_id ile eşleşiyor mu?
    team_id = team.get("team_id")
    for faction in FACTIONS:
        if team_id and team_id == (detail_teams.get(faction) or {}).get("faction_id"):
            return faction
    # 2) Oyuncuların çoğunluğu hangi faction'da?
    votes = [player_faction.get(p.get("player_id")) for p in team.get("players") or []]
    votes = [v for v in votes if v]
    if votes:
        return max(set(votes), key=votes.count)
    # 3) Son çare: sıraya göre
    return FACTIONS[idx] if idx < len(FACTIONS) else None


# --- yazma (upsert) ----------------------------------------------------------

def upsert_players(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    """Oyuncuları ekler/günceller; boş gelen alan eski değeri silmez."""
    for r in rows:
        con.execute(
            """
            INSERT INTO players (faceit_id, steam_id, nickname, elo, level, updated_at)
            VALUES ($faceit_id, $steam_id, $nickname, $elo, $level, now())
            ON CONFLICT (faceit_id) DO UPDATE SET
                steam_id   = COALESCE(excluded.steam_id, steam_id),
                nickname   = COALESCE(excluded.nickname, nickname),
                elo        = COALESCE(excluded.elo, elo),
                level      = COALESCE(excluded.level, level),
                updated_at = now()
            """,
            r,
        )


def upsert_match(con: duckdb.DuckDBPyConnection, row: dict[str, Any]) -> None:
    """Maçı ekler/günceller. İşlenmiş demonun durumu 'pending'e geri dönmez."""
    con.execute(
        """
        INSERT INTO matches (match_id, map, date, team_a, team_b, score, score_a, score_b,
                             winner, competition, demo_url, demo_status, updated_at)
        VALUES ($match_id, $map, $date, $team_a, $team_b, $score, $score_a, $score_b,
                $winner, $competition, $demo_url, $demo_status, now())
        ON CONFLICT (match_id) DO UPDATE SET
            map         = excluded.map,
            date        = excluded.date,
            team_a      = excluded.team_a,
            team_b      = excluded.team_b,
            score       = excluded.score,
            score_a     = excluded.score_a,
            score_b     = excluded.score_b,
            winner      = excluded.winner,
            competition = excluded.competition,
            demo_url    = COALESCE(excluded.demo_url, demo_url),
            demo_status = CASE
                WHEN demo_status IS NULL OR demo_status IN ('pending', 'missing')
                    THEN excluded.demo_status
                ELSE demo_status
            END,
            updated_at  = now()
        """,
        row,
    )


def upsert_player_stats(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    """Skor tablosunu yazar; demodan gelen kast/rating üzerine yazılmaz."""
    for r in rows:
        con.execute(
            """
            INSERT INTO match_player_stats (match_id, player_id, nickname, faction, team_name,
                kills, deaths, assists, kd_ratio, adr, hs_pct, mvps,
                triple_kills, quadro_kills, penta_kills, result, kast, rating)
            VALUES ($match_id, $player_id, $nickname, $faction, $team_name,
                $kills, $deaths, $assists, $kd_ratio, $adr, $hs_pct, $mvps,
                $triple_kills, $quadro_kills, $penta_kills, $result, $kast, $rating)
            ON CONFLICT (match_id, player_id) DO UPDATE SET
                nickname = excluded.nickname, faction = excluded.faction,
                team_name = excluded.team_name, kills = excluded.kills,
                deaths = excluded.deaths, assists = excluded.assists,
                kd_ratio = excluded.kd_ratio, adr = excluded.adr, hs_pct = excluded.hs_pct,
                mvps = excluded.mvps, triple_kills = excluded.triple_kills,
                quadro_kills = excluded.quadro_kills, penta_kills = excluded.penta_kills,
                result = excluded.result,
                kast = COALESCE(excluded.kast, kast),
                rating = COALESCE(excluded.rating, rating)
            """,
            r,
        )


def save_parsed_match(con: duckdb.DuckDBPyConnection, parsed: ParsedMatch) -> None:
    """Bir maçın tüm satırlarını tek transaction içinde yazar."""
    con.execute("BEGIN TRANSACTION")
    try:
        upsert_players(con, parsed.players)
        upsert_match(con, parsed.match)
        upsert_player_stats(con, parsed.player_stats)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# --- ana akış ----------------------------------------------------------------

@dataclass
class SyncResult:
    player_id: str
    nickname: str
    matches_seen: int = 0
    matches_written: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (match_id, neden)


def profile_to_player_row(profile: dict[str, Any], game: str = "cs2") -> dict[str, Any]:
    """Oyuncu profil yanıtını players satırına çevirir."""
    g = (profile.get("games") or {}).get(game) or {}
    return {
        "faceit_id": profile.get("player_id"),
        "steam_id": profile.get("steam_id_64") or g.get("game_player_id"),
        "nickname": profile.get("nickname"),
        "elo": _to_int(g.get("faceit_elo")),
        "level": _to_int(g.get("skill_level")),
    }


def sync_player_matches(
    client: FaceitClient,
    con: duckdb.DuckDBPyConnection,
    nickname: str,
    limit: int = 30,
    game: str = "cs2",
) -> SyncResult:
    """Oyuncunun son `limit` maçını çekip DB'ye upsert eder."""
    profile = client.get_player_by_nickname(nickname, game=game)
    player_row = profile_to_player_row(profile, game)
    upsert_players(con, [player_row])

    result = SyncResult(player_id=player_row["faceit_id"], nickname=player_row["nickname"] or nickname)
    history = client.get_player_history(result.player_id, limit=limit, game=game)
    result.matches_seen = len(history)

    for item in history:
        match_id = item.get("match_id")
        if not match_id:
            continue
        # İptal edilmiş / bitmemiş maçların istatistiği olmaz
        if str(item.get("status", "finished")).lower() != "finished":
            result.skipped.append((match_id, f"durum: {item.get('status')}"))
            continue
        try:
            details = client.get_match_details(match_id)
            if str(details.get("status", "")).upper() != "FINISHED":
                result.skipped.append((match_id, f"durum: {details.get('status')}"))
                continue
            stats = client.get_match_stats(match_id)
        except FaceitNotFound:
            result.skipped.append((match_id, "istatistik bulunamadı (404)"))
            continue

        save_parsed_match(con, parse_match(details, stats))
        result.matches_written += 1

    # Kadro upsert'i ana oyuncunun ELO'sunu silmez (COALESCE) ama emin olmak için tekrar yaz
    upsert_players(con, [player_row])
    return result
