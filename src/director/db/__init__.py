"""DuckDB bağlantısı ve şema kurulumu."""

from __future__ import annotations

from pathlib import Path

import duckdb

from director.config import DB_PATH

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | str = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """DuckDB dosyasına bağlanır; ":memory:" testler için desteklenir."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def init_db(con: duckdb.DuckDBPyConnection) -> list[str]:
    """schema.sql'i çalıştırır ve mevcut tablo adlarını döner."""
    con.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]
