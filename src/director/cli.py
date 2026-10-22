"""`director` komut satırı arayüzü (typer)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from director.config import DB_PATH
from director.db import connect, init_db
from director.faceit.client import FaceitAPIError, FaceitClient, FaceitNotFound
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


if __name__ == "__main__":
    app()
