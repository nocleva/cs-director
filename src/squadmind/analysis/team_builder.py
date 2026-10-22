"""Takım oluşturma: oyuncu önerisi, arama ve bir kadronun birlikte oynama dinamikleri.

Veri FACEIT skor tablolarından (match_player_stats: her maçta 10 oyuncu) ve işlenmiş
demolardan gelir. Bir oyuncunun maçları, o oyuncu ya da maçtaki başka bir kullanıcı siteye
katılıp geçmişini çektiyse DB'dedir; bu yüzden siteye katılmayan oyuncu için sayılar eksik
olabilir ve sayfa bunu söyler.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

import duckdb

# Harita başına önerilen en az demo (analiz bundan sonra "yeşil")
MIN_DEMOS = 10
# Bu sayıya kadar "kırmızı" (az), arası "turuncu"
LOW_DEMOS = 5
# Harita havuzu (yükleme seçimi ve kartlar; oyuncunun başka haritada demosu varsa o da görünür)
MAP_POOL = ("de_dust2", "de_mirage", "de_inferno", "de_nuke", "de_ancient", "de_anubis", "de_train")


def demo_level(n: int) -> str:
    """Demo sayısının durumu: ok (≥10, yeşil), mid (6-9, turuncu), low (≤5, kırmızı)."""
    if n >= MIN_DEMOS:
        return "ok"
    return "mid" if n > LOW_DEMOS else "low"


def map_label(map_name: str) -> str:
    return map_name.removeprefix("de_").capitalize()


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list, keys: tuple[str, ...]) -> list[dict]:
    return [dict(zip(keys, r, strict=True)) for r in con.execute(sql, params).fetchall()]


def played_with(con: duckdb.DuckDBPyConnection, faceit_id: str, limit: int = 8,
                exclude: list[str] | None = None, min_together: int = 2) -> list[dict[str, Any]]:
    """En çok aynı tarafta oynadığı oyuncular: birlikte maç ve kazanılan.

    Tek maçlık rastgele takım arkadaşları öneri değildir: en az `min_together` ortak maç.
    """
    return _rows(con, """
        SELECT b.player_id, any_value(b.nickname), count(DISTINCT a.match_id) AS n,
               count(DISTINCT a.match_id) FILTER (WHERE a.result) AS w, any_value(p.level)
        FROM match_player_stats a
        JOIN match_player_stats b ON b.match_id = a.match_id AND b.faction = a.faction
             AND b.player_id <> a.player_id
        LEFT JOIN players p ON p.faceit_id = b.player_id
        WHERE a.player_id = ? AND NOT list_contains(?, b.player_id)
        GROUP BY b.player_id HAVING count(DISTINCT a.match_id) >= ?
        ORDER BY n DESC, any_value(b.nickname) LIMIT ?
        """, [faceit_id, exclude or [], min_together, limit], ("faceit_id", "nickname", "together", "wins", "level"))


def search_players(con: duckdb.DuckDBPyConnection, q: str, limit: int = 10) -> list[dict[str, Any]]:
    """Nickname araması (büyük / küçük harf fark etmez): tam eşleşme, sonra baştan, sonra içinde."""
    q = q.strip()
    if len(q) < 2:
        return []
    return _rows(con, """
        WITH names AS (
            SELECT faceit_id, nickname, level FROM players WHERE nickname IS NOT NULL
            UNION
            SELECT player_id, nickname, NULL FROM match_player_stats
        )
        SELECT faceit_id, any_value(nickname) AS nick, max(level)
        FROM names WHERE nickname ILIKE '%' || ? || '%'
        GROUP BY faceit_id
        ORDER BY min(CASE WHEN lower(nickname) = lower(?) THEN 0
                          WHEN nickname ILIKE ? || '%' THEN 1 ELSE 2 END), nick
        LIMIT ?
        """, [q, q, q, limit], ("faceit_id", "nickname", "level"))


def member_stats(con: duckdb.DuckDBPyConnection, faceit_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Üye başına FACEIT skor tablosu özeti (DB'deki tüm maçları)."""
    rows = _rows(con, """
        SELECT player_id, count(*) AS n, avg(CASE WHEN result THEN 1.0 ELSE 0.0 END),
               sum(kills) / nullif(sum(deaths), 0), avg(adr), avg(hs_pct)
        FROM match_player_stats WHERE list_contains(?, player_id) GROUP BY player_id
        """, [faceit_ids], ("faceit_id", "matches", "win", "kd", "adr", "hs"))
    return {r["faceit_id"]: r for r in rows}


def pair_matrix(con: duckdb.DuckDBPyConnection, faceit_ids: list[str]) -> list[dict[str, Any]]:
    """Her üye ikilisi: aynı tarafta kaç maç, kaçı kazanıldı."""
    rows = _rows(con, """
        SELECT a.player_id, b.player_id, count(DISTINCT a.match_id),
               count(DISTINCT a.match_id) FILTER (WHERE a.result)
        FROM match_player_stats a
        JOIN match_player_stats b ON b.match_id = a.match_id AND b.faction = a.faction
        WHERE list_contains(?, a.player_id) AND list_contains(?, b.player_id)
              AND a.player_id < b.player_id
        GROUP BY a.player_id, b.player_id
        """, [faceit_ids, faceit_ids], ("a", "b", "together", "wins"))
    found = {(r["a"], r["b"]): r for r in rows}
    out = []
    for a, b in combinations(sorted(faceit_ids), 2):
        out.append(found.get((a, b), {"a": a, "b": b, "together": 0, "wins": 0}))
    return out


def team_maps(con: duckdb.DuckDBPyConnection, faceit_ids: list[str],
              min_members: int = 2) -> list[dict[str, Any]]:
    """Kadrodan en az `min_members` kişinin aynı tarafta olduğu maçlar, harita bazında."""
    return _rows(con, """
        WITH together AS (
            SELECT match_id, faction, bool_or(result) AS won, count(*) AS k
            FROM match_player_stats WHERE list_contains(?, player_id)
            GROUP BY match_id, faction HAVING count(*) >= ?
        )
        SELECT m.map, count(*) AS n, count(*) FILTER (WHERE t.won) AS w, max(t.k) AS most
        FROM together t JOIN matches m USING (match_id)
        GROUP BY m.map ORDER BY n DESC
        """, [faceit_ids, min_members], ("map", "matches", "wins", "most"))


def demo_counts(con: duckdb.DuckDBPyConnection, faceit_ids: list[str]) -> dict[str, dict[str, int]]:
    """Üye → harita → analize giren demolu maç sayısı."""
    out: dict[str, dict[str, int]] = {f: {} for f in faceit_ids}
    for fid, m, n in con.execute("""
        SELECT s.player_id, m.map, count(DISTINCT m.match_id)
        FROM match_player_stats s JOIN matches m USING (match_id)
        WHERE list_contains(?, s.player_id) AND m.demo_status = 'processed'
        GROUP BY 1, 2
        """, [faceit_ids]).fetchall():
        out[fid][m] = n
    return out
