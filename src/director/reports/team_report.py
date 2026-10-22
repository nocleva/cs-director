"""`director report team <nick>`: oyuncunun takımı için taktik ve ekonomi raporu.

Rapor yorum yapmaz, veriyi düzenler: her oran örneklemiyle verilir, az örnekli satırlar
işaretlenir. Yorum ve öneri `director` / `tactician` ajanlarının işi.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import polars as pl

from director.analysis.economy import (
    after_loss_decisions,
    buy_matchups,
    equipment_edge,
    pistol_conversion,
)
from director.analysis.maps import LANES
from director.analysis.player_metrics import load_demo_data
from director.analysis.team_tactics import (
    COMMIT_PLAYERS,
    DEFAULT_LANES,
    DEFAULT_WINDOW,
    MIN_ROUNDS,
    RUSH_SECONDS,
    ct_setups,
    lane_summary,
    pattern_summary,
    setup_summary,
    site_summary,
    t_round_patterns,
    team_rounds,
)

BUY_TR = {"pistol": "Pistol", "eco": "Eco", "force": "Force", "full": "Full buy"}
# Bu kadar round'un altında görülen CT dizilişleri "diğer" satırında toplanır
RARE_SETUP = 3
PATTERN_TR = {"rush": "Rush", "execute": "Execute", "split": "Split",
              "girilmedi": "Site'a girilmedi"}


@dataclass
class TeamReport:
    nickname: str
    markdown: str
    matches: int
    rounds: int


def _pct(num: float, den: float) -> str:
    """'%53 (28/53)'; payda 0 ise '-'."""
    return "-" if not den else f"%{100 * num / den:.0f} ({num:.0f}/{den:.0f})"


def _few(n: int) -> str:
    return " ⚠️ az örnek" if n < MIN_ROUNDS else ""


def _teammates(d, steam_id: str) -> list[tuple[str, int]]:
    """Oyuncuyla aynı takımda en çok oynayanlar: (nickname, maç)."""
    mine = d.roster.filter(pl.col("steam_id") == steam_id).select("match_id", "faction")
    return (
        d.roster.join(mine, on=["match_id", "faction"])
        .filter(pl.col("steam_id") != steam_id)
        .group_by("nickname").agg(pl.col("match_id").n_unique().alias("n"))
        .sort(["n", "nickname"], descending=[True, False])
        .rows()
    )


def build_team_report(con: duckdb.DuckDBPyConnection, nickname: str,
                      map_name: str | None = None) -> TeamReport:
    """Takım raporunu Markdown olarak üretir."""
    row = con.execute(
        "SELECT steam_id, nickname FROM players WHERE lower(nickname) = lower(?)", [nickname]
    ).fetchone()
    if not row:
        raise LookupError(f"'{nickname}' veritabanında yok. Önce `director sync {nickname}`.")
    steam_id, nick = row

    d = load_demo_data(con, map_name)
    tr = team_rounds(d, steam_id)
    scope = f" — {map_name.removeprefix('de_')}" if map_name else ""
    lines = [f"# Takım raporu: {nick} ve takımı{scope}", "",
             f"_Oluşturulma: {date.today():%d.%m.%Y}_", ""]
    if tr.height == 0:
        lines.append("Bu oyuncu için demo verisi olan maç yok.")
        return TeamReport(nick, "\n".join(lines), 0, 0)

    n_matches = tr["match_id"].n_unique()
    t, ct = tr.filter(pl.col("side") == "T"), tr.filter(pl.col("side") == "CT")
    mates = _teammates(d, steam_id)
    maps = tr.group_by("map").agg(pl.col("match_id").n_unique()).sort("match_id",
                                                                     descending=True)

    # --- örneklem
    lines += [
        "## Örneklem", "",
        f"- **{n_matches} maç, {tr.height} round** ({t.height} T, {ct.height} CT). Haritalar: "
        + ", ".join(f"{m.removeprefix('de_')} ({n})" for m, n in maps.rows()),
        f"- Takım = {nick} ile her maçta aynı takımda oynayanlar. FACEIT'te kadro maçtan "
        "maça değişebilir; desenler bu maçlardaki takımların ortak eğilimidir.",
        "- En sık takım arkadaşları: " + (", ".join(f"{m} ({n} maç)" for m, n in mates[:6])
                                         or "-"),
        *([f"- Çekirdek kadro: {len(core)} oyuncu maçların en az yarısında birlikte "
           f"({', '.join(core)}); desenler büyük ölçüde bu kadronun eğilimi."]
          if (core := [m for m, n in mates if n * 2 >= n_matches]) else []),
        "",
        "## Genel", "",
        "| | Kazanma |", "|---|---|",
        f"| Tüm round'lar | {_pct(tr['won'].sum(), tr.height)} |",
        f"| T | {_pct(t['won'].sum(), t.height)} |",
        f"| CT | {_pct(ct['won'].sum(), ct.height)} |",
        "",
    ]

    # --- T tarafı desenleri (koridor bilgisi olan haritalar)
    tactical_maps = [m for m in maps["map"].to_list() if m in LANES]
    if tactical_maps:
        tp = t_round_patterns(d, steam_id).filter(pl.col("map").is_in(tactical_maps))
        lines += _t_section(tp)
        lines += _ct_section(ct_setups(d, steam_id).filter(pl.col("map").is_in(tactical_maps)))
    else:
        lines += ["## Taktik desenleri", "",
                  "Bu haritalar için koridor / alan tanımı yok (`analysis/maps.py`); "
                  "desen tespiti yapılmadı.", ""]

    lines += _site_section(tr)
    lines += _economy_section(tr)
    return TeamReport(nick, "\n".join(lines), n_matches, tr.height)


def _t_section(tp: pl.DataFrame) -> list[str]:
    ps = pattern_summary(tp)
    defaults = tp["default"].fill_null(False).sum()
    lines = [
        "## T tarafı: desenler", "",
        f"Her T round'u konum kayıtlarından sınıflandırıldı. Giriş anı: en az "
        f"{COMMIT_PLAYERS} T'nin aynı anda hedef site'ın giriş alanında olduğu ilk an "
        f"(plant edilip bu an görülmediyse plant anı). **Rush:** giriş ≤{RUSH_SECONDS:.0f}. "
        "sn. **Split:** daha geç giriş, oyuncular ≥2 farklı koridordan geldi. **Execute:** "
        "daha geç giriş, tek koridor. **Site'a girilmedi:** save, erken ölüm ya da site'a "
        "girmeden biten round (taktik hatası anlamına gelmez). "
        f"**Default'tan:** {DEFAULT_WINDOW[0]:.0f}-{DEFAULT_WINDOW[1]:.0f}. sn'de T'ler "
        f"≥{DEFAULT_LANES} koridora yayılmıştı ({defaults}/{tp.height} round).", "",
        "| Desen | Site | Round | Kazanma | Plant | Plant sonrası kazanma | Default'tan "
        "| Medyan giriş (sn) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in ps.iter_rows(named=True):
        commit = "-" if r["commit_median"] is None else f"{r['commit_median']:.0f}"
        lines.append(
            f"| {PATTERN_TR.get(r['pattern'], r['pattern'])} | {r['target_site'] or '-'} | "
            f"{r['rounds']}{_few(r['rounds'])} | {_pct(r['wins'], r['rounds'])} | "
            f"{_pct(r['plants'], r['rounds'])} | {_pct(r['plant_wins'], r['plants'])} | "
            f"{r['from_default']} | {commit} |"
        )
    lines.append("")

    ls = lane_summary(tp)
    if ls.height:
        lines += ["### Giriş rotaları", "",
                  "Giriş anı etrafında (±10 sn) site'a giren oyuncuların son kullandığı "
                  "koridorlar.", "",
                  "| Desen | Site | Rota | Round | Kazanma |", "|---|---|---|---|---|"]
        for r in ls.iter_rows(named=True):
            lines.append(f"| {PATTERN_TR.get(r['pattern'], r['pattern'])} | "
                         f"{r['target_site']} | {r['route']} | {r['rounds']}{_few(r['rounds'])} "
                         f"| {_pct(r['wins'], r['rounds'])} |")
        lines.append("")

    # Alım tipine göre: eco / force round'ların deseni farklı olur
    by_buy = (tp.group_by("own_buy", "pattern").agg(pl.len().alias("n"),
                                                    pl.col("won").sum().alias("w"))
              .sort("own_buy", "pattern"))
    lines += ["### Alım tipine göre desen", "", "| Alım | Desen | Round | Kazanma |",
              "|---|---|---|---|"]
    for buy, pattern, n, w in by_buy.rows():
        lines.append(f"| {BUY_TR.get(buy, buy)} | {PATTERN_TR.get(pattern, pattern)} | {n} | "
                     f"{_pct(w, n)} |")
    return lines + [""]


def _ct_section(cs: pl.DataFrame) -> list[str]:
    ss = setup_summary(cs)
    known = int(cs["setup"].is_not_null().sum())
    lines = [
        "## CT tarafı: dizilişler", "",
        "15-35. saniyede her CT'nin en çok bulunduğu alan; **A-Mid-B** sayıları (ör. 2-2-1 = "
        "A'da 2, Mid'de 2, B'de 1; Mid'e CT spawn, catwalk ve alt tünel dahil). Sadece 5 CT'nin "
        f"de yerinin bilindiği round'lar: {known}/{cs.height}.", "",
        "| Diziliş (A-Mid-B) | Round | Kazanma |", "|---|---|---|",
    ]
    # Nadir dizilişler (RARE_SETUP round altı) tek satırda
    common, rare = ss.filter(pl.col("rounds") >= RARE_SETUP), ss.filter(pl.col("rounds") < RARE_SETUP)
    for setup, n, w in common.rows():
        lines.append(f"| {setup} | {n}{_few(n)} | {_pct(w, n)} |")
    if rare.height:
        n, w = int(rare["rounds"].sum()), int(rare["wins"].sum())
        lines.append(f"| diğer ({rare.height} diziliş: {', '.join(rare['setup'].to_list())}) "
                     f"| {n}{_few(n)} | {_pct(w, n)} |")
    return lines + [""]


def _site_section(tr: pl.DataFrame) -> list[str]:
    ss = site_summary(tr)
    lines = ["## Plant ve retake", "",
             "| Taraf | Site | Plant | Plant / round | Plant'tan sonra kazanma | Medyan plant (sn) |",
             "|---|---|---|---|---|---|"]
    for r in ss.iter_rows(named=True):
        who = "T (biz plant ettik)" if r["side"] == "T" else "CT (rakip plant etti → retake)"
        lines.append(f"| {who} | {r['bomb_site']} | {r['plants']}{_few(r['plants'])} | "
                     f"{_pct(r['plants'], r['side_rounds'])} | {_pct(r['wins'], r['plants'])} | "
                     f"{r['plant_median']:.0f} |")
    return lines + [""]


def _economy_section(tr: pl.DataFrame) -> list[str]:
    lines = ["## Ekonomi", "",
             "Alım tipleri awpy'nin sınıflaması (takımın freeze sonundaki ekipmanına göre). "
             "Pistol kazanıldıktan sonraki SMG + armor alımı da \"force\" sayılır.", "",
             "### Alım eşleşmeleri", "", "| Biz | Rakip | Round | Kazanma |", "|---|---|---|---|"]
    for own, enemy, n, w in buy_matchups(tr).rows():
        lines.append(f"| {BUY_TR.get(own, own)} | {BUY_TR.get(enemy, enemy)} | {n}{_few(n)} | "
                     f"{_pct(w, n)} |")

    pc = {r["pistol_won"]: r for r in pistol_conversion(tr).iter_rows(named=True)}
    lines += ["", "### Pistol ve sonrası", "",
              "| Pistol | Yarı | 2. round kazanma | 3. round kazanma | 2. round force |",
              "|---|---|---|---|---|"]
    for won, label in ((True, "Kazanıldı"), (False, "Kaybedildi")):
        r = pc.get(won)
        if r is None:
            continue
        force = "-" if won else _pct(r["r2_force"], r["halves"])
        lines.append(f"| {label} | {r['halves']} | {_pct(r['r2_wins'], r['halves'])} | "
                     f"{_pct(r['r3_wins'], r['halves'])} | {force} |")

    lines += ["", "### Kayıptan sonraki alım kararı", "",
              "Bir round kaybedildikten sonra (pistol round'ları hariç) takımın alımı; karar o "
              "round ve **sonraki round** ile birlikte değerlendirilir (eco o round'u feda edip "
              "sonrakinde full buy sağlar).", "",
              "| Karar | Round | O round kazanma | Sonraki round kazanma | Sonraki round full buy |",
              "|---|---|---|---|---|"]
    for r in after_loss_decisions(tr).iter_rows(named=True):
        lines.append(f"| {BUY_TR.get(r['own_buy'], r['own_buy'])} | {r['rounds']}{_few(r['rounds'])} "
                     f"| {_pct(r['wins'], r['rounds'])} | {_pct(r['next_wins'], r['next_rounds'])} "
                     f"| {_pct(r['next_full'], r['next_rounds'])} |")

    lines += ["", "### Ekipman farkına göre kazanma", "",
              "Freeze sonundaki takım ekipman değeri farkı (biz − rakip), pistol hariç.", "",
              "| Fark | Round | Kazanma |", "|---|---|---|"]
    for edge, n, w in equipment_edge(tr).rows():
        lines.append(f"| {edge} | {n}{_few(n)} | {_pct(w, n)} |")
    return lines + [""]
