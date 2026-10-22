"""Demo inbox'ı → açma → parse → DuckDB (Faz 1b).

Akış: data/demos/inbox/ içindeki .dem / .dem.zst / .dem.gz dosyası açılır, awpy ile
ayrıştırılır, hangi FACEIT maçına ait olduğu bulunur, tablolar yazılır, orijinal
dosya processed/'a taşınır ve geçici açılmış .dem silinir.
"""

from __future__ import annotations

import gzip
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import polars as pl
import zstandard

from director.parse.demo_parser import PARSER_VERSION, DemoTables, parse_demo

# FACEIT demo dosya adı maç ID'siyle başlar: "1-<uuid>-1-1.dem.zst"
MATCH_ID_RE = re.compile(r"^(1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")

DEMO_SUFFIXES = (".dem", ".dem.zst", ".dem.gz")

# Dosya adında maç ID'si yoksa: demodaki oyunculardan en az bu kadarı maçın kadrosunda olmalı
MIN_ROSTER_OVERLAP = 8

# FACEIT skor tablosu ile demo arasında kabul edilen fark
KILL_TOLERANCE = 1
ADR_TOLERANCE = 5.0


# --- dosya işlemleri ---------------------------------------------------------

def match_id_from_filename(name: str) -> str | None:
    m = MATCH_ID_RE.match(name)
    return m.group(1) if m else None


def find_demos(folder: Path) -> list[Path]:
    """Klasördeki demo dosyaları (isim sırasına göre)."""
    if not folder.exists():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.name.lower().endswith(DEMO_SUFFIXES)
    )


def decompress(src: Path, tmp_dir: Path) -> Path:
    """Sıkıştırılmış demoyu tmp_dir'e açar; zaten .dem ise olduğu gibi döner."""
    name = src.name.lower()
    if name.endswith(".dem"):
        return src
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = tmp_dir / re.sub(r"\.(zst|gz)$", "", src.name, flags=re.IGNORECASE)
    with src.open("rb") as fi, out.open("wb") as fo:
        if name.endswith(".zst"):
            zstandard.ZstdDecompressor().copy_stream(fi, fo)
        else:
            with gzip.open(fi) as gz:
                shutil.copyfileobj(gz, fo)
    return out


# --- maç eşleştirme ----------------------------------------------------------

def resolve_match_id(con: duckdb.DuckDBPyConnection, demo_file: Path,
                     tables: DemoTables) -> str | None:
    """Önce dosya adından, olmazsa harita + kadro örtüşmesinden maç ID'si bulur."""
    mid = match_id_from_filename(demo_file.name)
    if mid:
        return mid
    steam_ids = [s for s in tables.players["steamid"].to_list() if s]
    if not steam_ids:
        return None
    row = con.execute(
        """
        SELECT m.match_id, count(*) AS overlap
        FROM matches m
        JOIN match_player_stats s USING (match_id)
        JOIN players p ON p.faceit_id = s.player_id
        WHERE m.map = ? AND list_contains(?, p.steam_id)
        GROUP BY m.match_id
        ORDER BY overlap DESC, max(m.date) DESC
        LIMIT 1
        """,
        [tables.map_name, steam_ids],
    ).fetchone()
    if row and row[1] >= MIN_ROSTER_OVERLAP:
        return row[0]
    return None


# --- DB yazma ----------------------------------------------------------------

def _insert(con: duckdb.DuckDBPyConnection, table: str, match_id: str, df: pl.DataFrame) -> int:
    """DataFrame'i match_id kolonu ekleyerek tabloya yazar (kolon adlarıyla eşler)."""
    if df.height == 0:
        return 0
    frame = df.with_columns(pl.lit(match_id).alias("match_id"))
    cols = ", ".join(frame.columns)
    con.register("_frame", frame)
    try:
        con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _frame")
    finally:
        con.unregister("_frame")
    return frame.height


DEMO_TABLES = ("rounds", "kills", "utility", "positions_snapshot", "demo_player_stats",
               "damages")


def discard_demo_data(con: duckdb.DuckDBPyConnection, match_id: str) -> None:
    """Güvenilmeyen demonun verilerini DB'den çıkarır, maçı 'mismatch' olarak işaretler."""
    con.execute("BEGIN TRANSACTION")
    try:
        for table in DEMO_TABLES:
            con.execute(f"DELETE FROM {table} WHERE match_id = ?", [match_id])
        con.execute("UPDATE match_player_stats SET kast = NULL WHERE match_id = ?", [match_id])
        con.execute(
            "UPDATE matches SET demo_status = 'mismatch', updated_at = now() WHERE match_id = ?",
            [match_id],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def save_demo_tables(con: duckdb.DuckDBPyConnection, match_id: str, tables: DemoTables) -> None:
    """Bir maçın demo tablolarını yazar. Aynı maç tekrar işlenirse önce eskisini siler."""
    con.execute("BEGIN TRANSACTION")
    try:
        for table in DEMO_TABLES:
            con.execute(f"DELETE FROM {table} WHERE match_id = ?", [match_id])

        _insert(con, "rounds", match_id, tables.rounds)
        _insert(con, "kills", match_id, tables.kills)
        _insert(con, "utility", match_id, tables.utility)
        _insert(con, "positions_snapshot", match_id, tables.positions)
        _insert(con, "demo_player_stats", match_id, tables.player_stats)
        if tables.damages is not None:
            _insert(con, "damages", match_id, tables.damages)

        # Maç DB'de yoksa (sync yapılmamış) en azından harita ile kayıt aç
        con.execute(
            """
            INSERT INTO matches (match_id, map, demo_status, parser_version)
            VALUES (?, ?, 'processed', ?)
            ON CONFLICT (match_id) DO UPDATE SET demo_status = 'processed',
                parser_version = excluded.parser_version, updated_at = now()
            """,
            [match_id, tables.map_name, PARSER_VERSION],
        )

        # Her round'da CT olan takım (faction): o round'daki CT konumlarının çoğunluğu
        con.execute(
            """
            UPDATE rounds SET ct_faction = sub.faction
            FROM (
                SELECT ps.round_no, s.faction,
                       row_number() OVER (PARTITION BY ps.round_no ORDER BY count(*) DESC) AS rn
                FROM positions_snapshot ps
                JOIN players p ON p.steam_id = ps.player
                JOIN match_player_stats s ON s.match_id = ps.match_id AND s.player_id = p.faceit_id
                WHERE ps.match_id = ? AND ps.side = 'CT'
                GROUP BY ps.round_no, s.faction
            ) sub
            WHERE rounds.match_id = ? AND rounds.round_no = sub.round_no AND sub.rn = 1
            """,
            [match_id, match_id],
        )

        # Demodan gelen KAST'ı FACEIT skor tablosuna işle (FACEIT bu alanı vermez)
        con.execute(
            """
            UPDATE match_player_stats SET kast = d.kast
            FROM demo_player_stats d
            JOIN players p ON p.steam_id = d.steam_id
            WHERE d.match_id = ? AND match_player_stats.match_id = d.match_id
              AND match_player_stats.player_id = p.faceit_id
            """,
            [match_id],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# --- doğrulama ---------------------------------------------------------------

@dataclass
class ValidationRow:
    nickname: str
    faceit_kills: int | None
    demo_kills: int | None
    faceit_deaths: int | None
    demo_deaths: int | None
    faceit_adr: float | None
    demo_adr: float | None

    @property
    def ok(self) -> bool:
        def close(a, b, tol) -> bool:
            return a is None or b is None or abs(a - b) <= tol

        return (
            close(self.faceit_kills, self.demo_kills, KILL_TOLERANCE)
            and close(self.faceit_deaths, self.demo_deaths, KILL_TOLERANCE)
            and close(self.faceit_adr, self.demo_adr, ADR_TOLERANCE)
        )


def validate_against_faceit(con: duckdb.DuckDBPyConnection, match_id: str) -> list[ValidationRow]:
    """Demo istatistiklerini FACEIT skor tablosuyla oyuncu oyuncu karşılaştırır."""
    rows = con.execute(
        """
        SELECT s.nickname, s.kills, d.kills, s.deaths, d.deaths, s.adr, d.adr
        FROM match_player_stats s
        JOIN players p ON p.faceit_id = s.player_id
        LEFT JOIN demo_player_stats d ON d.match_id = s.match_id AND d.steam_id = p.steam_id
        WHERE s.match_id = ?
        ORDER BY s.kills DESC
        """,
        [match_id],
    ).fetchall()
    return [ValidationRow(*r) for r in rows]


# --- ana akış ----------------------------------------------------------------

@dataclass
class DemoIngestResult:
    file: str
    match_id: str | None = None
    map_name: str | None = None
    rounds: int = 0
    kills: int = 0
    utility: int = 0
    positions: int = 0
    validation: list[ValidationRow] = field(default_factory=list)
    error: str | None = None
    deleted: bool = False  # demo silindi (verileri DB'de)
    freed_bytes: int = 0
    kept_reason: str | None = None  # silme istendi ama dosya saklandıysa nedeni

    @property
    def validated(self) -> bool | None:
        """FACEIT ile tutarlı mı; FACEIT verisi yoksa None."""
        if not self.validation:
            return None
        return all(v.ok for v in self.validation)


def ingest_demo(
    con: duckdb.DuckDBPyConnection,
    demo_file: Path,
    tmp_dir: Path,
    processed_dir: Path,
    parser: Callable[[Path], DemoTables] = parse_demo,
    delete_after: bool = False,
) -> DemoIngestResult:
    """Tek bir demoyu işler. Hata olursa dosya inbox'ta kalır, sonuçta hata yazar.

    delete_after=True ise, veriler DB'ye yazılıp FACEIT ile tutarlı çıktıktan sonra
    orijinal demo silinir. Tutarsızlık varsa incelenebilsin diye processed/'a taşınır.
    """
    result = DemoIngestResult(file=demo_file.name)
    unpacked: Path | None = None
    try:
        unpacked = decompress(demo_file, tmp_dir)
        tables = parser(unpacked)
        result.map_name = tables.map_name

        match_id = resolve_match_id(con, demo_file, tables)
        if not match_id:
            result.error = "maç bulunamadı (önce `director sync` çalıştır)"
            return result
        result.match_id = match_id

        save_demo_tables(con, match_id, tables)
        result.rounds = tables.rounds.height
        result.kills = tables.kills.height
        result.utility = tables.utility.height
        result.positions = tables.positions.height
        result.validation = validate_against_faceit(con, match_id)

        # FACEIT ile tutmuyorsa veriye güvenme: analizleri kirletmesin diye DB'den çıkar
        if result.validated is False:
            discard_demo_data(con, match_id)

        if delete_after:
            reason = keep_reason(result)
            if reason is None:
                # Veriler DB'de ve doğrulandı: demo dosyasına artık gerek yok
                result.freed_bytes = demo_file.stat().st_size
                demo_file.unlink()
                result.deleted = True
                return result
            result.kept_reason = reason

        # Saklanacak: orijinal dosyayı processed/'a taşı (zaten oradaysa dokunma)
        processed_dir.mkdir(parents=True, exist_ok=True)
        if demo_file.parent.resolve() != processed_dir.resolve():
            shutil.move(str(demo_file), str(processed_dir / demo_file.name))
    except Exception as exc:  # demo bozuk / parse hatası: diğer demolar devam etsin
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        # Açılmış büyük .dem dosyasını sil (orijinal sıkıştırılmış dosya kalır)
        if unpacked is not None and unpacked != demo_file and unpacked.exists():
            unpacked.unlink()
    return result


def keep_reason(result: DemoIngestResult) -> str | None:
    """Demo silinmemeli mi? Silinmemesi gerekiyorsa nedenini döner."""
    if result.rounds == 0 or result.kills == 0:
        return "demodan veri çıkmadı"
    if result.validated is False:
        return "FACEIT ile fark var; veriler DB'ye alınmadı, demo inceleme için saklandı"
    return None


def processed_demos_in_db(con: duckdb.DuckDBPyConnection,
                          processed_dir: Path) -> list[tuple[Path, int, int]]:
    """processed/ içinde olup verileri DB'de bulunan demolar: (dosya, round, kill)."""
    out = []
    for f in find_demos(processed_dir):
        mid = match_id_from_filename(f.name)
        if not mid:
            continue
        rounds, kills = con.execute(
            "SELECT (SELECT count(*) FROM rounds WHERE match_id = $m), "
            "(SELECT count(*) FROM kills WHERE match_id = $m)",
            {"m": mid},
        ).fetchone()
        if rounds > 0 and kills > 0:
            out.append((f, rounds, kills))
    return out


def ingest_inbox(
    con: duckdb.DuckDBPyConnection,
    inbox: Path,
    processed_dir: Path,
    tmp_dir: Path,
    parser: Callable[[Path], DemoTables] = parse_demo,
    on_start: Callable[[Path], None] | None = None,
    delete_after: bool = False,
) -> list[DemoIngestResult]:
    """Inbox'taki tüm demoları sırayla işler."""
    results = []
    for demo_file in find_demos(inbox):
        if on_start:
            on_start(demo_file)
        results.append(
            ingest_demo(con, demo_file, tmp_dir, processed_dir, parser, delete_after)
        )
    return results
