"""Web arayüzünün veri katmanı: tek DuckDB bağlantısı + kilit, kullanıcı ve yükleme kayıtları.

DuckDB bir dosyaya aynı anda tek yazar kabul eder; web istekleri ve arka plan işçisi aynı
süreçte çalıştığı için hepsi bu sınıftaki tek bağlantıyı kilitle sırayla kullanır.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import duckdb

from squadmind.db import connect, init_db

T = TypeVar("T")

# Yükleme durumları (arayüzde gösterilen Türkçe adlar)
UPLOADING, QUEUED, PROCESSING = "yükleniyor", "sırada", "işleniyor"
DONE, REJECTED, FAILED, MISMATCH = "tamam", "reddedildi", "hata", "tutmadı"
OPEN_STATES = (UPLOADING, QUEUED, PROCESSING)


@dataclass
class WebUser:
    steam_id: str
    faceit_id: str
    nickname: str
    sync_status: str
    team_access: bool = False   # takım sahibinin kadro sayfaları (takım raporu, taktik tahtası)


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self._con = connect(db_path)
        init_db(self._con)
        self._lock = threading.Lock()

    @contextmanager
    def cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Kilitli imleç: aynı anda tek kullanıcı."""
        with self._lock:
            cur = self._con.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def run(self, fn: Callable[[duckdb.DuckDBPyConnection], T], lock: bool = True) -> T:
        """Bir fonksiyonu (rapor üretici, ingest) kendi imleciyle çalıştırır.

        lock=False: uzun işler (FACEIT senkronu ~1 dk, demo işleme ~4 sn, rapor) web
        sayfalarını dondurmasın diye kilitsiz. DuckDB aynı süreçteki ayrı imleçleri paralel
        çalıştırır; işçi tek iş parçacığı olduğu için işçi işleri birbiriyle çakışmaz, web
        istekleri ise yalnızca web_* tablolarına yazar.
        """
        if lock:
            with self.cursor() as cur:
                return fn(cur)
        cur = self._con.cursor()
        try:
            return fn(cur)
        finally:
            cur.close()

    def close(self) -> None:
        self._con.close()

    # --- kullanıcılar -------------------------------------------------------------

    def add_user(self, steam_id: str, faceit_id: str, nickname: str,
                 team_access: bool = False) -> None:
        """Kullanıcıyı ekler / adını günceller; kadro erişimi bir kez verilince geri alınmaz."""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO web_users (steam_id, faceit_id, nickname, sync_status, team_access)
                VALUES (?, ?, ?, 'bekliyor', ?)
                ON CONFLICT (steam_id) DO UPDATE SET nickname = excluded.nickname,
                    team_access = web_users.team_access OR excluded.team_access
                """,
                [steam_id, faceit_id, nickname, team_access],
            )

    def user(self, steam_id: str) -> WebUser | None:
        with self.cursor() as cur:
            row = cur.execute("SELECT steam_id, faceit_id, nickname, sync_status, coalesce(team_access, false) FROM web_users "
                              "WHERE steam_id = ?", [steam_id]).fetchone()
        return WebUser(*row) if row else None

    def users(self) -> list[WebUser]:
        with self.cursor() as cur:
            rows = cur.execute("SELECT steam_id, faceit_id, nickname, sync_status, coalesce(team_access, false) FROM web_users "
                               "ORDER BY joined_at").fetchall()
        return [WebUser(*r) for r in rows]

    def set_sync_status(self, steam_id: str, status: str) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE web_users SET sync_status = ? WHERE steam_id = ?",
                        [status, steam_id])

    # --- yüklemeler ---------------------------------------------------------------

    def new_upload(self, steam_id: str, file_name: str, size: int,
                   map_hint: str | None = None) -> str:
        upload_id = uuid.uuid4().hex
        with self.cursor() as cur:
            cur.execute("INSERT INTO web_uploads (id, steam_id, file_name, size_bytes, status, "
                        "map_hint) VALUES (?, ?, ?, ?, ?, ?)",
                        [upload_id, steam_id, file_name, size, UPLOADING, map_hint])
        return upload_id

    def upload(self, upload_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute("SELECT id, steam_id, file_name, size_bytes, status, match_id, "
                              "message, map_hint FROM web_uploads WHERE id = ?",
                              [upload_id]).fetchone()
        if not row:
            return None
        keys = ("id", "steam_id", "file_name", "size_bytes", "status", "match_id", "message",
                "map_hint")
        return dict(zip(keys, row, strict=True))

    def uploads_of(self, steam_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT u.id, u.file_name, u.size_bytes, u.status, u.match_id, u.message, "
                "u.created_at, coalesce(m.map, u.map_hint) FROM web_uploads u "
                "LEFT JOIN matches m USING (match_id) WHERE u.steam_id = ? "
                "ORDER BY u.created_at DESC", [steam_id]
            ).fetchall()
        keys = ("id", "file_name", "size_bytes", "status", "match_id", "message", "created_at",
                "map")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def open_uploads(self, steam_id: str) -> int:
        with self.cursor() as cur:
            return cur.execute("SELECT count(*) FROM web_uploads WHERE steam_id = ? "
                               "AND list_contains(?, status)",
                               [steam_id, list(OPEN_STATES)]).fetchone()[0]

    def set_upload(self, upload_id: str, status: str, message: str | None = None,
                   match_id: str | None = None) -> None:
        finished = status not in OPEN_STATES
        with self.cursor() as cur:
            cur.execute(
                "UPDATE web_uploads SET status = ?, message = coalesce(?, message), "
                "match_id = coalesce(?, match_id), "
                "finished_at = CASE WHEN ? THEN now() ELSE finished_at END WHERE id = ?",
                [status, message, match_id, finished, upload_id],
            )

    def done_count(self, steam_id: str) -> int:
        """Bu oyuncunun analize giren (FACEIT ile doğrulanmış) demo sayısı.

        Web'den yüklenenler + demosu başka bir takım arkadaşı tarafından zaten eklenmiş
        maçları sayar: aynı maçın demosu herkes için bir kez yüklenir.
        """
        with self.cursor() as cur:
            return cur.execute(
                """
                SELECT count(DISTINCT m.match_id)
                FROM matches m
                JOIN match_player_stats s USING (match_id)
                JOIN players p ON p.faceit_id = s.player_id
                WHERE p.steam_id = ? AND m.demo_status = 'processed'
                  AND (? IS NULL OR m.map = ?)
                """,
                [steam_id, self.map_name, self.map_name],
            ).fetchone()[0]

    map_name: str | None = None  # done_count'un saydığı harita (uygulama ayarlar)

    def demo_counts(self, steam_id: str) -> dict[str, int]:
        """Harita → bu oyuncunun analize giren (doğrulanmış demolu) maç sayısı.

        Kim yüklerse yüklesin: aynı maçın demosu herkes için bir kez yüklenir.
        """
        with self.cursor() as cur:
            rows = cur.execute(
                """
                SELECT m.map, count(DISTINCT m.match_id)
                FROM matches m
                JOIN match_player_stats s USING (match_id)
                JOIN players p ON p.faceit_id = s.player_id
                WHERE p.steam_id = ? AND m.demo_status = 'processed'
                GROUP BY m.map
                """, [steam_id]).fetchall()
        return {m: n for m, n in rows}

    # --- takımlar -------------------------------------------------------------------

    def create_team(self, name: str, owner: WebUser) -> str:
        team_id = uuid.uuid4().hex[:12]
        with self.cursor() as cur:
            cur.execute("INSERT INTO web_teams (id, name, owner_steam_id) VALUES (?, ?, ?)",
                        [team_id, name, owner.steam_id])
            cur.execute("INSERT INTO web_team_members (team_id, faceit_id, nickname, status, "
                        "added_by) VALUES (?, ?, ?, 'üye', ?)",
                        [team_id, owner.faceit_id, owner.nickname, owner.steam_id])
        return team_id

    def team(self, team_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute("SELECT id, name, owner_steam_id, created_at FROM web_teams "
                              "WHERE id = ?", [team_id]).fetchone()
        return dict(zip(("id", "name", "owner_steam_id", "created_at"), row, strict=True)) if row else None

    def team_members(self, team_id: str) -> list[dict[str, Any]]:
        """Üyeler: siteye katılıp katılmadığı (steam_id) ve FACEIT seviyesiyle."""
        with self.cursor() as cur:
            rows = cur.execute(
                """
                SELECT t.faceit_id, coalesce(w.nickname, p.nickname, t.nickname), t.status,
                       w.steam_id IS NOT NULL, p.steam_id, p.level, p.elo
                FROM web_team_members t
                LEFT JOIN players p ON p.faceit_id = t.faceit_id
                LEFT JOIN web_users w ON w.faceit_id = t.faceit_id
                WHERE t.team_id = ? ORDER BY t.added_at
                """, [team_id]).fetchall()
        keys = ("faceit_id", "nickname", "status", "joined", "steam_id", "level", "elo")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def teams_of(self, faceit_id: str) -> list[dict[str, Any]]:
        """Oyuncunun üyesi olduğu ya da davet edildiği takımlar (üye sayısıyla)."""
        with self.cursor() as cur:
            rows = cur.execute(
                """
                SELECT t.id, t.name, me.status,
                       (SELECT count(*) FROM web_team_members x WHERE x.team_id = t.id) AS n
                FROM web_teams t JOIN web_team_members me ON me.team_id = t.id
                WHERE me.faceit_id = ? ORDER BY t.created_at DESC
                """, [faceit_id]).fetchall()
        return [dict(zip(("id", "name", "status", "members"), r, strict=True)) for r in rows]

    def add_member(self, team_id: str, faceit_id: str, nickname: str, added_by: str) -> None:
        with self.cursor() as cur:
            cur.execute("INSERT INTO web_team_members (team_id, faceit_id, nickname, status, "
                        "added_by) VALUES (?, ?, ?, 'davet', ?) ON CONFLICT DO NOTHING",
                        [team_id, faceit_id, nickname, added_by])

    def set_member_status(self, team_id: str, faceit_id: str, status: str) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE web_team_members SET status = ? WHERE team_id = ? AND faceit_id = ?",
                        [status, team_id, faceit_id])

    def remove_member(self, team_id: str, faceit_id: str) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM web_team_members WHERE team_id = ? AND faceit_id = ?",
                        [team_id, faceit_id])

    def totals(self, steam_id: str) -> dict[str, int]:
        """Genel bakış sayıları: oyuncunun analize giren demo / round'u ve sitenin toplamı."""
        with self.cursor() as cur:
            mine = cur.execute(
                """
                WITH mm AS (
                    SELECT DISTINCT m.match_id FROM matches m
                    JOIN match_player_stats s USING (match_id)
                    JOIN players p ON p.faceit_id = s.player_id
                    WHERE p.steam_id = ? AND m.demo_status = 'processed'
                )
                SELECT (SELECT count(*) FROM mm),
                       (SELECT count(*) FROM rounds r WHERE r.match_id IN (SELECT match_id FROM mm))
                """, [steam_id]).fetchone()
            site = cur.execute(
                "SELECT count(*), (SELECT count(*) FROM rounds) FROM matches "
                "WHERE demo_status = 'processed'").fetchone()
        return {"demos": mine[0], "rounds": mine[1], "site_demos": site[0], "site_rounds": site[1]}

    # --- desen doğrulama ------------------------------------------------------------

    def save_review(self, match_id: str, round_no: int, label: str, target_site: str | None,
                    verdict: str, corrected: str | None, note: str | None, reviewer: str) -> None:
        """Bir round'un etiket kararı; aynı kişi yeniden işaretlerse üzerine yazılır."""
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO label_reviews (match_id, round_no, label, target_site, verdict,
                                           corrected, note, reviewer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (match_id, round_no, reviewer) DO UPDATE SET
                    label = excluded.label, target_site = excluded.target_site,
                    verdict = excluded.verdict, corrected = excluded.corrected,
                    note = excluded.note, reviewed_at = now()
                """, [match_id, round_no, label, target_site, verdict, corrected, note, reviewer])

    def reviews(self, reviewer: str | None = None) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT match_id, round_no, label, target_site, verdict, corrected, note, reviewer, "
                "reviewed_at FROM label_reviews WHERE ? IS NULL OR reviewer = ? "
                "ORDER BY reviewed_at", [reviewer, reviewer]).fetchall()
        keys = ("match_id", "round_no", "label", "target_site", "verdict", "corrected", "note",
                "reviewer", "reviewed_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    # --- taktik oluşturucu -----------------------------------------------------------

    def save_tactic(self, owner: str, name: str, map_name: str, side: str, data: str,
                    tactic_id: str | None = None) -> str:
        """Yeni taktik ya da (aynı sahibin) mevcut taktiğini günceller; kimliği döner."""
        with self.cursor() as cur:
            if tactic_id and cur.execute("SELECT 1 FROM user_tactics WHERE id = ? AND owner = ?",
                                         [tactic_id, owner]).fetchone():
                cur.execute("UPDATE user_tactics SET name = ?, map = ?, side = ?, data = ?, "
                            "updated_at = now() WHERE id = ?", [name, map_name, side, data, tactic_id])
                return tactic_id
            new_id = uuid.uuid4().hex[:12]
            cur.execute("INSERT INTO user_tactics (id, owner, name, map, side, data) "
                        "VALUES (?, ?, ?, ?, ?, ?)", [new_id, owner, name, map_name, side, data])
            return new_id

    def tactics_of(self, owner: str) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rows = cur.execute("SELECT id, name, map, side, data, updated_at FROM user_tactics "
                               "WHERE owner = ? ORDER BY updated_at DESC", [owner]).fetchall()
        keys = ("id", "name", "map", "side", "data", "updated_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def delete_tactic(self, owner: str, tactic_id: str) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM user_tactics WHERE id = ? AND owner = ?", [tactic_id, owner])
