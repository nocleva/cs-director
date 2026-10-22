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
from director.analysis.team_management import (
    NEAR_DIST,
    OTHER,
    TRADE_SECONDS,
    core_roster,
    player_spots,
    proximity_pairs,
    spot_ranking,
    support_needs,
    team_vs_opponents,
    trade_pairs,
)
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

    # --- takım yönetimi: kim nerede iyi, güç farkı, kime destek, uyum
    tactical_maps = [m for m in maps["map"].to_list() if m in LANES]
    lines += _management_section(d, steam_id, bool(tactical_maps))

    # --- T tarafı desenleri (koridor bilgisi olan haritalar)
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


# --- takım yönetimi ------------------------------------------------------------------

# Güç farkı tablosunda gösterilen metrikler: (anahtar, etiket, tür, iyi yön)
VERSUS_METRICS = [
    ("t_open_win", "T: opening kazanma", "pct", "high"),
    ("ct_open_win", "CT: opening kazanma", "pct", "high"),
    ("kpr", "Kill / round", "ratio", "high"),
    ("adr", "ADR", "num", "high"),
    ("traded_death", "Ölümün trade edilmesi", "pct", "high"),
    ("isolated_death", "Yalnız ölüm", "pct", "low"),
    ("util_dmg_per_round", "Utility hasarı / round", "num", "high"),
    ("flashes_per_round", "Flash / round", "ratio", "high"),
    ("enemies_per_flash", "Flash başı kör edilen rakip", "ratio", "high"),
    ("team_flash_per_flash", "Flash başı kör edilen takım arkadaşı", "ratio", "low"),
    ("flash_assists_per_round", "Flash assist / round", "ratio", "high"),
    ("clutch_win", "Clutch kazanma", "pct", "high"),
]


def _rate_text(r, kind: str) -> str:
    v = r.value
    if v is None:
        return "-"
    if kind == "pct":
        return _pct(r.num, r.den)
    return f"{v:.1f}" if kind == "num" else f"{v:.2f}"


def _versus_flag(a, b, kind: str, better: str) -> str:
    """Belirgin fark işareti: yüzdelerde ≥5 puan, diğerlerinde ≥%15 (oyuncu raporuyla aynı)."""
    if a.value is None or not b.value:
        return ""
    diff = a.value - b.value
    big = abs(diff) >= 0.05 if kind == "pct" else abs(diff) / abs(b.value) >= 0.15
    if not big:
        return ""
    return "güçlü" if (diff > 0) == (better == "high") else "zayıf"


def _management_section(d, steam_id: str, has_places: bool) -> list[str]:
    roster = core_roster(d, steam_id)
    names = roster["nickname"].to_list()
    lines = ["## Takım yönetimi", "",
             f"Çekirdek kadro: **{', '.join(names)}** (maçların en az yarısında birlikte). "
             f"Diğer takım arkadaşları \"{OTHER}\" olarak gruplandı.", ""]

    if has_places:
        ranked = spot_ranking(player_spots(d, steam_id))
        lines += [
            "### Kim nerede iyi", "",
            "CT: 15-35. saniyede en çok bulunulan alan; T: 15-20. saniyedeki koridor "
            "(default kurulumu). O round'lardaki tüm kill / ölümler sayılır. **Net/R** = "
            "(kill − ölüm) / round; sıra bu değere göre. **Lobi** = aynı maçlardaki herkes "
            "(referans). Bir oyuncu birden çok alanda önde olabilir; kim nereye atanır, "
            "tactician'ın işi.", "",
        ]
        for side, title in (("CT", "CT alanları (A / Mid / B)"), ("T", "T koridorları")):
            part = ranked.filter(pl.col("side") == side)
            lines += [f"#### {title}", "",
                      "| Alan | Sıra | Oyuncu | Round | Round kazanma | Kill/R | Ölüm/R | Net/R "
                      "| Opening (K-Ö) |", "|---|---|---|---|---|---|---|---|---|"]
            for r in part.iter_rows(named=True):
                n = r["rounds"]
                rank = "-" if r["rank"] is None else str(r["rank"])
                lines.append(
                    f"| {r['spot']} | {rank} | {r['player']} | {n}{_few(n)} | "
                    f"{_pct(r['wins'], n)} | {r['kills'] / n:.2f} | {r['deaths'] / n:.2f} | "
                    f"{r['net']:+.2f} | {r['ok']}-{r['od']} |")
            lines.append("")

    # Güç farkı: takım ve aynı maçlardaki rakipler
    team, opp = team_vs_opponents(d, steam_id)
    lines += ["### Güç farkımız (takım vs aynı maçlardaki rakipler)", "",
              "Belirgin = yüzdelerde en az 5 puan, diğerlerinde en az %15 fark.", "",
              "| Metrik | Biz | Rakip | |", "|---|---|---|---|"]
    for key, label, kind, better in VERSUS_METRICS:
        a, b = team[key], opp[key]
        lines.append(f"| {label} | {_rate_text(a, kind)} | {_rate_text(b, kind)} | "
                     f"{_versus_flag(a, b, kind, better)} |")
    lines.append("")

    # Kime destek gerekli
    sn = support_needs(d, steam_id)
    lines += ["### Kime destek gerekli", "",
              "Oyuncu başına ölümler: yalnız (en yakın hayattaki takım arkadaşı >900 birim), "
              "trade edilen ve round'un ilk ölümü (opening). Yalnız ölüm oranı yüksek, trade "
              "oranı düşük oyuncu destek bekliyor demektir.", "",
              "| Oyuncu | Taraf | Round | Ölüm / round | Yalnız ölüm | Trade edildi | Opening ölümü |",
              "|---|---|---|---|---|---|---|"]
    for r in sn.iter_rows(named=True):
        lines.append(
            f"| {r['player']} | {r['side']} | {r['rounds']} | {r['deaths'] / r['rounds']:.2f} | "
            f"{_pct(r['isolated'], r['measured'])} | {_pct(r['traded'], r['deaths'])} | "
            f"{_pct(r['opening_deaths'], r['rounds'])} |")
    lines.append("")

    # Uyum: kim kimin ölümünü trade ediyor, kim kiminle yakın oynuyor
    tp = trade_pairs(d, steam_id)
    lines += ["### Uyum: kim kimi trade ediyor", "",
              f"Takım arkadaşını öldüren rakibi {TRADE_SECONDS:.0f} saniye içinde öldürmek. "
              "Satır = trade eden, sütun = trade edilen (ölen).", ""]
    people = names + [OTHER]
    lines += ["| Trade eden \\ ölen | " + " | ".join(people) + " |",
              "|---|" + "---|" * len(people)]
    counts = {(a, b): n for a, b, n in tp.rows()}
    for a in people:
        cells = ["-" if a == b and a != OTHER else str(counts.get((a, b), 0)) for b in people]
        lines.append(f"| **{a}** | " + " | ".join(cells) + " |")
    lines.append("")

    if has_places:
        pp = proximity_pairs(d, steam_id)
        lines += ["### Uyum: kim kiminle yakın oynuyor", "",
                  f"İkisi de hayattayken aralarındaki mesafenin ≤{NEAR_DIST:.0f} birim olduğu "
                  "konum kayıtlarının payı (CT 15-35. sn, T 15-45. sn).", "",
                  "| Oyuncular | CT'de yakın | T'de yakın |", "|---|---|---|"]
        wide = {(r["a"], r["b"], r["side"]): r for r in pp.iter_rows(named=True)}
        for a, b in sorted({(r["a"], r["b"]) for r in pp.iter_rows(named=True)}):
            ct, t = wide.get((a, b, "CT")), wide.get((a, b, "T"))
            lines.append(f"| {a} + {b} | "
                         f"{_pct(ct['near'], ct['samples']) if ct else '-'} | "
                         f"{_pct(t['near'], t['samples']) if t else '-'} |")
        lines.append("")
    return lines
