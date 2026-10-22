"""DuckDB bağlantısı ve şema kurulumu."""

from __future__ import annotations

from pathlib import Path

import duckdb

from director.config import DB_PATH

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Şema sürümü. v2: demo tabloları awpy'nin gerçek kolonlarına göre yeniden kuruldu.
# v3: kill'e wallbang (penetrated, noscope), maça parser_version, hasar tablosu.
# v4: demo_files (aynı demo bir kez: içerik özeti, boyut, yükleyen).
SCHEMA_VERSION = 4

# v3'te mevcut tablolara eklenen kolonlar (eski DB'ler için)
_V3_COLUMNS = (
    ("kills", "penetrated", "INTEGER"),
    ("kills", "noscope", "BOOLEAN"),
    ("matches", "parser_version", "INTEGER"),
)

# v1'deki demo tabloları; v1'de hiç doldurulmadıkları için yeniden kurmak güvenli
_V1_DEMO_TABLES = ("rounds", "kills", "utility", "positions_snapshot")


def connect(db_path: Path | str = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """DuckDB dosyasına bağlanır; ":memory:" testler için desteklenir."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def _current_version(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    row = con.execute("SELECT max(version) FROM schema_version").fetchone()
    return row[0] or 0


def init_db(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Şemayı kurar / günceller ve mevcut tablo adlarını döner."""
    version = _current_version(con)
    if version < 2:
        # v1 demo tabloları eski kolonlarla; boş oldukları için silip yeniden kur
        for table in _V1_DEMO_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    if version < 3:
        for table, column, typ in _V3_COLUMNS:
            con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {typ}")
    if version < SCHEMA_VERSION:
        con.execute("INSERT INTO schema_version VALUES (?)", [SCHEMA_VERSION])
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]
