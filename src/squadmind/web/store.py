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

    def add_user(self, steam_id: str, faceit_id: str, nickname: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO web_users (steam_id, faceit_id, nickname, sync_status)
                VALUES (?, ?, ?, 'bekliyor')
                ON CONFLICT (steam_id) DO UPDATE SET nickname = excluded.nickname
                """,
                [steam_id, faceit_id, nickname],
            )

    def user(self, steam_id: str) -> WebUser | None:
        with self.cursor() as cur:
            row = cur.execute("SELECT steam_id, faceit_id, nickname, sync_status FROM web_users "
                              "WHERE steam_id = ?", [steam_id]).fetchone()
        return WebUser(*row) if row else None

    def users(self) -> list[WebUser]:
        with self.cursor() as cur:
            rows = cur.execute("SELECT steam_id, faceit_id, nickname, sync_status FROM web_users "
                               "ORDER BY joined_at").fetchall()
        return [WebUser(*r) for r in rows]

    def set_sync_status(self, steam_id: str, status: str) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE web_users SET sync_status = ? WHERE steam_id = ?",
                        [status, steam_id])

    # --- yüklemeler ---------------------------------------------------------------

    def new_upload(self, steam_id: str, file_name: str, size: int) -> str:
        upload_id = uuid.uuid4().hex
        with self.cursor() as cur:
            cur.execute("INSERT INTO web_uploads (id, steam_id, file_name, size_bytes, status) "
                        "VALUES (?, ?, ?, ?, ?)", [upload_id, steam_id, file_name, size, UPLOADING])
        return upload_id

    def upload(self, upload_id: str) -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute("SELECT id, steam_id, file_name, size_bytes, status, match_id, "
                              "message FROM web_uploads WHERE id = ?", [upload_id]).fetchone()
        if not row:
            return None
        keys = ("id", "steam_id", "file_name", "size_bytes", "status", "match_id", "message")
        return dict(zip(keys, row, strict=True))

    def uploads_of(self, steam_id: str) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT id, file_name, size_bytes, status, match_id, message, created_at "
                "FROM web_uploads WHERE steam_id = ? ORDER BY created_at DESC", [steam_id]
            ).fetchall()
        keys = ("id", "file_name", "size_bytes", "status", "match_id", "message", "created_at")
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
