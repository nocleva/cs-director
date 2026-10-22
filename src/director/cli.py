"""`director` komut satırı arayüzü (typer)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from director.config import (
    AUTO_DELETE_DEMOS,
    DB_PATH,
    DEMOS_INBOX,
    DEMOS_PROCESSED,
    DEMOS_TMP,
    REPORTS_DIR,
)
from director.db import connect, init_db
from director.faceit.client import FaceitAPIError, FaceitClient, FaceitNotFound
from director.ingest.demos import (
    find_demos,
    ingest_inbox,
    match_id_from_filename,
    processed_demos_in_db,
)
from director.ingest.matches import profile_to_player_row, sync_player_matches

app = typer.Typer(help="CS2 FACEIT Gaming Director", no_args_is_help=True)
console = Console()

DbOption = typer.Option(DB_PATH, "--db", help="DuckDB dosya yolu")


def _make_client() -> FaceitClient:
    """API anahtarı eksikse anlaşılır mesajla çık."""
    try:
        return FaceitClient()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@app.command("init-db")
def init_db_cmd(db: Path = DbOption) -> None:
    """DuckDB şemasını oluşturur (tekrar çalıştırmak güvenli)."""
    with connect(db) as con:
        tables = init_db(con)
    console.print(f"[green]Veritabanı hazır:[/green] {db}")
    console.print("Tablolar: " + ", ".join(tables))


@app.command("fetch-player")
def fetch_player(nickname: str, game: str = typer.Option("cs2", help="Oyun kodu")) -> None:
    """FACEIT profilini çeker: ELO, level, steam id."""
    with _make_client() as client:
        try:
            profile = client.get_player_by_nickname(nickname, game=game)
        except FaceitNotFound:
            console.print(f"[red]'{nickname}' bulunamadı (nickname büyük/küçük harfe duyarlı).[/red]")
            raise typer.Exit(1)
        except FaceitAPIError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        cache_file = client.cache_path("/players", {"nickname": nickname, "game": game})

    row = profile_to_player_row(profile, game)
    table = Table(title=f"FACEIT profili: {row['nickname']}", show_header=False)
    table.add_row("Player ID", str(row["faceit_id"]))
    table.add_row("Steam ID64", str(row["steam_id"]))
    table.add_row("Ülke", str(profile.get("country")))
    table.add_row("Bölge", str(((profile.get("games") or {}).get(game) or {}).get("region")))
    table.add_row("ELO", str(row["elo"]))
    table.add_row("Level", str(row["level"]))
    console.print(table)
    # Ham yanıtın şeklini incelemek için cache dosyasını göster
    console.print(f"[dim]Ham yanıt: {cache_file}[/dim]")


@app.command()
def sync(
    nickname: str,
    limit: int = typer.Option(30, "--limit", "-n", min=1, help="Kaç maç çekilsin"),
    db: Path = DbOption,
) -> None:
    """Son N maçı ve skor tablolarını DB'ye çeker (tekrar çekmek kopya oluşturmaz)."""
    with _make_client() as client, connect(db) as con:
        init_db(con)
        try:
            with console.status(f"{nickname} için son {limit} maç çekiliyor..."):
                result = sync_player_matches(client, con, nickname, limit=limit)
        except FaceitNotFound:
            console.print(f"[red]'{nickname}' bulunamadı.[/red]")
            raise typer.Exit(1)
        except FaceitAPIError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        pending = con.execute(
            "SELECT count(*) FROM matches WHERE demo_status = 'pending'"
        ).fetchone()[0]

    console.print(
        f"[green]{result.matches_written}/{result.matches_seen} maç yazıldı[/green] "
        f"({result.nickname}). Demo bekleyen maç: {pending}"
    )
    for match_id, reason in result.skipped:
        console.print(f"  [yellow]atlandı[/yellow] {match_id}: {reason}")


@app.command()
def summary(
    nickname: str,
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Kaç maç gösterilsin"),
    db: Path = DbOption,
) -> None:
    """DB'deki son maçların özet tablosu: harita, skor, K/D, ADR, HS%, sonuç."""
    if not Path(db).exists():
        console.print("[red]Veritabanı yok. Önce `director sync <nickname>` çalıştır.[/red]")
        raise typer.Exit(1)

    with connect(db, read_only=True) as con:
        rows = con.execute(
            """
            SELECT m.date, m.map, m.score_a, m.score_b, s.faction,
                   s.kills, s.deaths, s.kd_ratio, s.adr, s.hs_pct, s.result
            FROM match_player_stats s
            JOIN matches m USING (match_id)
            JOIN players p ON p.faceit_id = s.player_id
            WHERE lower(p.nickname) = lower(?)
            ORDER BY m.date DESC
            LIMIT ?
            """,
            [nickname, limit],
        ).fetchall()

    if not rows:
        console.print(f"[yellow]{nickname} için DB'de maç yok. `director sync {nickname}` dene.[/yellow]")
        raise typer.Exit(1)

    table = Table(title=f"{nickname} — son {len(rows)} maç")
    for col in ("Tarih", "Harita", "Skor", "K-D", "K/D", "ADR", "HS%", "Sonuç"):
        table.add_column(col, justify="right" if col not in ("Tarih", "Harita") else "left")

    wins = 0
    kd_vals: list[float] = []
    adr_vals: list[float] = []
    hs_vals: list[float] = []
    for date, map_name, sa, sb, faction, k, d, kd, adr, hs, won in rows:
        # Skoru oyuncunun kendi takımı önce gelecek şekilde göster
        own, opp = (sa, sb) if faction == "faction1" else (sb, sa)
        score = f"{own}-{opp}" if own is not None and opp is not None else "?"
        wins += 1 if won else 0
        if kd is not None:
            kd_vals.append(kd)
        if adr is not None:
            adr_vals.append(adr)
        if hs is not None:
            hs_vals.append(hs)
        table.add_row(
            date.strftime("%Y-%m-%d") if date else "?",
            (map_name or "?").removeprefix("de_"),
            score,
            f"{k}-{d}",
            f"{kd:.2f}" if kd is not None else "-",
            f"{adr:.1f}" if adr is not None else "-",
            f"{hs:.0f}" if hs is not None else "-",
            "[green]G[/green]" if won else "[red]M[/red]",
        )

    def _avg(values: list[float]) -> str:
        return f"{sum(values) / len(values):.2f}" if values else "-"

    table.add_section()
    table.add_row(
        "Ortalama", "", f"%{100 * wins / len(rows):.0f} G", "",
        _avg(kd_vals), _avg(adr_vals), _avg(hs_vals), f"{wins}/{len(rows)}",
    )
    console.print(table)


def local_demo_match_ids(*folders: Path) -> set[str]:
    """inbox/processed klasörlerindeki demo dosyalarından maç ID'lerini çıkarır."""
    ids: set[str] = set()
    for folder in folders:
        if not folder.exists():
            continue
        for f in folder.iterdir():
            mid = match_id_from_filename(f.name)
            if mid:
                ids.add(mid)
    return ids


@app.command("demo-links")
def demo_links(
    nickname: str,
    map_name: str = typer.Option(None, "--map", help="Sadece bu harita (ör. dust2)"),
    db: Path = DbOption,
) -> None:
    """Demosu henüz indirilmemiş maçların FACEIT sayfa linklerini Markdown'a yazar."""
    have = local_demo_match_ids(DEMOS_INBOX, DEMOS_PROCESSED)
    with connect(db, read_only=True) as con:
        rows = con.execute(
            """
            SELECT m.match_id, m.date, m.map, m.score_a, m.score_b, s.faction, s.result
            FROM matches m
            JOIN match_player_stats s USING (match_id)
            JOIN players p ON p.faceit_id = s.player_id
            WHERE lower(p.nickname) = lower(?) AND m.demo_url IS NOT NULL
              AND (? IS NULL OR m.map = 'de_' || lower(?) OR m.map = lower(?))
            ORDER BY m.date DESC
            """,
            [nickname, map_name, map_name, map_name],
        ).fetchall()

    todo = [r for r in rows if r[0] not in have]
    lines = [
        f"# İndirilecek demolar — {nickname}",
        "",
        f"Elde olan: {len(rows) - len(todo)} · İndirilecek: {len(todo)}. "
        "En yeniden eskiye; FACEIT eski demoları sildiği için üstten başla.",
        f"İndirdiğin `.dem.zst` dosyalarını `{DEMOS_INBOX}` içine at.",
        "",
        "| # | Tarih | Harita | Skor | Sonuç | Maç sayfası |",
        "|---|---|---|---|---|---|",
    ]
    for i, (mid, date, mp, sa, sb, faction, won) in enumerate(todo, 1):
        own, opp = (sa, sb) if faction == "faction1" else (sb, sa)
        lines.append(
            f"| {i} | {date:%Y-%m-%d} | {(mp or '?').removeprefix('de_')} | {own}-{opp} | "
            f"{'G' if won else 'M'} | [aç](https://www.faceit.com/tr/cs2/room/{mid}) |"
        )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / "demo_links.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]{len(todo)} maç listelendi[/green] (elde olan {len(rows) - len(todo)}): {out}")


def _mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB"


@app.command("ingest-demos")
def ingest_demos(
    details: bool = typer.Option(False, "--details", help="Oyuncu bazında FACEIT karşılaştırması"),
    delete: bool = typer.Option(
        AUTO_DELETE_DEMOS, "--delete/--keep",
        help="İşlenip doğrulanan demoyu sil / sakla (varsayılan: .env DIRECTOR_AUTO_DELETE)",
    ),
    db: Path = DbOption,
) -> None:
    """Inbox'taki demoları açar, ayrıştırır, DB'ye yazar; sonra siler ya da processed/'a taşır."""
    pending = find_demos(DEMOS_INBOX)
    if not pending:
        console.print(f"[yellow]İşlenecek demo yok:[/yellow] {DEMOS_INBOX}")
        return

    with connect(db) as con:
        init_db(con)
        with console.status("Demolar işleniyor...") as status:
            results = ingest_inbox(
                con, DEMOS_INBOX, DEMOS_PROCESSED, DEMOS_TMP,
                on_start=lambda f: status.update(f"İşleniyor: {f.name}"),
                delete_after=delete,
            )

    table = Table(title=f"Demo işleme — {len(results)} dosya")
    for col in ("Maç", "Harita", "Round", "Kill", "Utility", "FACEIT ile", "Demo dosyası"):
        table.add_column(col)
    for r in results:
        if r.error:
            table.add_row(r.match_id or r.file[:24], r.map_name or "?", "", "", "",
                          f"[red]{r.error}[/red]", "inbox'ta kaldı")
            continue
        check = {True: "[green]tutarlı[/green]", False: "[yellow]fark var[/yellow]",
                 None: "[dim]FACEIT verisi yok[/dim]"}[r.validated]
        if r.deleted:
            fate = "[green]silindi[/green] (veriler öğrenildi)"
        elif r.kept_reason:
            fate = f"[yellow]saklandı[/yellow]: {r.kept_reason}"
        else:
            fate = "processed/'a taşındı"
        table.add_row(r.match_id[:14] + "…", (r.map_name or "").removeprefix("de_"),
                      str(r.rounds), str(r.kills), str(r.utility), check, fate)
    console.print(table)

    deleted = [r for r in results if r.deleted]
    if deleted:
        console.print(
            f"[green]{len(deleted)} demo silindi, {_mb(sum(r.freed_bytes for r in deleted))} "
            "yer açıldı.[/green] Bu maçların round, kill, utility ve konum verileri "
            "veritabanına kaydedildi ve FACEIT ile doğrulandı; analiz için demo dosyasına "
            "artık gerek yok."
        )

    # İstenirse ya da fark varsa oyuncu bazında karşılaştırmayı göster
    for r in results:
        if r.validation and (details or r.validated is False):
            t = Table(title=f"{r.match_id} — FACEIT / demo")
            for col in ("Oyuncu", "Kill", "Death", "ADR", ""):
                t.add_column(col)
            for v in r.validation:
                adr = (f"{v.faceit_adr:.1f} / {v.demo_adr:.1f}"
                       if v.faceit_adr is not None and v.demo_adr is not None else "-")
                t.add_row(v.nickname, f"{v.faceit_kills} / {v.demo_kills}",
                          f"{v.faceit_deaths} / {v.demo_deaths}", adr,
                          "[green]✓[/green]" if v.ok else "[yellow]![/yellow]")
            console.print(t)


@app.command("clean-demos")
def clean_demos(
    yes: bool = typer.Option(False, "--yes", "-y", help="Onay sormadan sil"),
    db: Path = DbOption,
) -> None:
    """processed/ içindeki, verileri zaten DB'de olan demo dosyalarını siler."""
    if not Path(db).exists():
        console.print("[red]Veritabanı yok.[/red]")
        raise typer.Exit(1)
    with connect(db, read_only=True) as con:
        learned = processed_demos_in_db(con, DEMOS_PROCESSED)
    if not learned:
        console.print("[yellow]Silinecek, verisi DB'de olan demo yok.[/yellow]")
        return

    total = sum(f.stat().st_size for f, _, _ in learned)
    for f, rounds, kills in learned:
        console.print(f"  {f.name[:40]}  → DB'de {rounds} round, {kills} kill")
    if not yes and not typer.confirm(
        f"{len(learned)} demo ({_mb(total)}) silinsin mi? Verileri veritabanında kalacak."
    ):
        raise typer.Exit()

    for f, _, _ in learned:
        f.unlink()
    console.print(
        f"[green]{len(learned)} demo silindi, {_mb(total)} yer açıldı.[/green] "
        "Maçların verileri öğrenildi ve veritabanında duruyor."
    )


if __name__ == "__main__":
    app()
