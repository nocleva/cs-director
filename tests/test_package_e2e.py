"""Gerçek demoyla uçtan uca paket testi (demo, Node ve işlenmiş maçlı DB kopyası yoksa atlanır).

Akış: işlenmiş bir demo Node + WASM ile paketlenir; geçici DB kopyasında (canlı DB'ye dokunulmaz)
1) maç zaten işlenmiş → paket reddedilir, 2) maçın tabloları silinince paket onları eski
tablolarla AYNI yeniden yazar.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import polars as pl
import pytest

from squadmind.db import connect, init_db
from squadmind.ingest.demos import DEMO_TABLES, ingest_package

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = Path(os.environ.get("SQUADMIND_PROCESSED_DEMOS",
                                r"C:\Users\furka\squadmind-data\demos\processed"))
DB_COPY = Path(os.environ.get("SQUADMIND_E2E_DB", ""))  # işlenmiş maçları olan DB (kopyalanır)
NODE = shutil.which("node")


def _first_demo() -> Path | None:
    if not PROCESSED.exists():
        return None
    demos = sorted(PROCESSED.glob("*.dem.zst"))
    return demos[0] if demos else None


pytestmark = pytest.mark.skipif(
    not (NODE and _first_demo() and DB_COPY.is_file()),
    reason="gerçek demo, Node ya da SQUADMIND_E2E_DB (işlenmiş maçlı DB) yok")


def _snapshot(con, match_id: str) -> dict[str, pl.DataFrame]:
    out = {}
    for t in DEMO_TABLES:
        df = pl.from_arrow(con.cursor().sql(
            f"SELECT * FROM {t} WHERE match_id = ?", params=[match_id]).to_arrow_table())
        out[t] = df.sort(df.columns)
    return out


def test_real_demo_package_roundtrip(tmp_path):
    demo = _first_demo()
    match_id = demo.name[:38]
    db = tmp_path / "copy.duckdb"
    shutil.copy(DB_COPY, db)
    con = connect(db)
    init_db(con)  # kopya eski şemadaysa v9'a taşır (migration de böylece denenir)
    if not con.execute("SELECT count(*) FROM rounds WHERE match_id = ?", [match_id]).fetchone()[0]:
        pytest.skip("bu demonun maçı DB kopyasında işlenmemiş")

    pack_file = tmp_path / "p.json.gz"
    subprocess.run([NODE, str(ROOT / "wasm" / "awpy-wasm" / "node_pack.js"), str(demo), str(pack_file)],
                   check=True, capture_output=True, timeout=600)
    pack = pack_file.read_bytes()
    sha = hashlib.sha256(demo.read_bytes()).hexdigest()
    before = _snapshot(con, match_id)

    # 1) Maç zaten işlenmiş: paket reddedilir
    r = ingest_package(con, pack, demo.name, sha)
    assert r.rejected and "zaten işlendi" in r.error

    # 2) Maç DB'den silinirse paketle aynı tablolar yeniden yazılır
    for t in DEMO_TABLES:
        con.execute(f"DELETE FROM {t} WHERE match_id = ?", [match_id])
    con.execute("UPDATE matches SET demo_status = 'pending' WHERE match_id = ?", [match_id])
    con.execute("DELETE FROM demo_files WHERE match_id = ?", [match_id])
    r = ingest_package(con, pack, demo.name, sha, orig_size=demo.stat().st_size)
    assert r.error is None and r.mismatch_reason is None and r.validated is True, r
    after = _snapshot(con, match_id)
    for t in DEMO_TABLES:
        assert before[t].equals(after[t]), f"{t} eski tablolarla aynı değil"
    src = con.execute("SELECT source FROM demo_files WHERE sha256 = ?", [sha]).fetchone()[0]
    assert src == "paket"
    json.dumps({"ok": True})
