"""`director report player <nick>`: oyuncu için Markdown rapor.

Rapor yorum yapmaz, veriyi düzenler: her metrik oyuncunun değeri, aynı maçlardaki
diğer oyuncuların (lobi) değeri ve örneklem büyüklüğüyle verilir. Yorum ve öneri
Faz 1d'de `director` ajanının işi; bu rapor onun girdisidir.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import polars as pl

from director.analysis.player_metrics import (
    DemoData,
    Rate,
    aggregate,
    economy_breakdown,
    load_demo_data,
    place_breakdown,
    player_match_metrics,
    weapon_breakdown,
    weapon_exposure,
    weapon_place_breakdown,
)
from director.analysis.roles import (
    AWP_THRESHOLD,
    ROLE_SIGNALS,
    primary_roles,
    role_scores,
    side_roles,
)

# Bu kadar örneklemin altındaki oranlar "az örneklem" diye işaretlenir ve
# güçlü/zayıf yön listesine girmez
MIN_SAMPLE = 20
# ... ve en az bu kadar olay (pay) gerekir: 9 flash assist'ten sonuç çıkarılmaz
MIN_EVENTS = 10
# Güçlü / zayıf yön sayılması için lobiden en az bu kadar fark:
# yüzdelerde mutlak puan, diğer metriklerde göreli oran
MIN_PCT_POINTS = 0.05
MIN_REL_DIFF = 0.15
# Silah maruziyeti tablosunda gösterilecek en az round; bunun altı gösterilmez,
# EXPOSURE_FEW altı "az örnek" diye işaretlenir
EXPOSURE_MIN_ROUNDS = 5
EXPOSURE_FEW = 15
SNIPERS = ("awp", "scout")


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    kind: str            # pct / num / sec / dist / ratio
    better: str | None   # high / low / None (yönsüz)


SECTIONS: dict[str, list[Metric]] = {
    "Genel": [
        Metric("round_win", "Round kazanma", "pct", "high"),
        Metric("t_round_win", "T round kazanma", "pct", "high"),
        Metric("ct_round_win", "CT round kazanma", "pct", "high"),
        Metric("pistol_win", "Pistol round kazanma", "pct", "high"),
        Metric("kd", "K/D", "ratio", "high"),
        Metric("adr", "ADR", "num", "high"),
        Metric("kast", "KAST", "pct", "high"),
        Metric("hs", "Headshot oranı", "pct", None),
        Metric("sniper_share", "AWP + Scout kill payı", "pct", None),
        Metric("dpr", "Round başı ölüm", "ratio", "low"),
        Metric("multikill", "3+ kill'li round", "pct", "high"),
        Metric("clutch_win", "Clutch kazanma", "pct", "high"),
    ],
    "Opening duel": [
        Metric("t_open_attempt", "T: opening'e girme", "pct", None),
        Metric("t_open_win", "T: opening kazanma", "pct", "high"),
        Metric("ct_open_attempt", "CT: opening'e girme", "pct", None),
        Metric("ct_open_win", "CT: opening kazanma", "pct", "high"),
    ],
    "Trade": [
        Metric("traded_death", "Ölümün trade edilmesi", "pct", "high"),
        Metric("trade_kill", "Kill'in trade kill olması", "pct", None),
    ],
    "Utility": [
        Metric("util_per_round", "Round başı utility", "ratio", None),
        Metric("flashes_per_round", "Round başı flash", "ratio", None),
        Metric("smokes_per_round", "Round başı smoke", "ratio", None),
        Metric("enemies_per_flash", "Flash başı kör edilen rakip", "ratio", "high"),
        Metric("team_flash_per_flash", "Flash başı kör edilen takım arkadaşı", "ratio", "low"),
        Metric("flash_assists_per_round", "Round başı flash assist", "ratio", "high"),
        Metric("util_dmg_per_round", "Round başı utility hasarı", "num", "high"),
    ],
    "Zamanlama ve konum": [
        Metric("t_first_contact", "T: ilk temas (sn)", "sec", None),
        Metric("ct_first_contact", "CT: ilk temas (sn)", "sec", None),
        Metric("death_early", "İlk 20 sn'de ölüm", "pct", None),
        Metric("death_late", "60. sn sonrası ölüm", "pct", None),
        Metric("lurk_dist", "T: takım merkezine uzaklık", "dist", None),
        Metric("ct_anchor", "CT: aynı site'ı tutma", "pct", None),
        Metric("ct_rotation", "CT: round içinde site değiştirme", "pct", None),
    ],
}

SIDE_TR = {"T": "T", "CT": "CT"}
BUY_TR = {"pistol": "Pistol", "eco": "Eco", "force": "Force", "full": "Full buy"}


def _fmt(r: Rate, kind: str, with_sample: bool = False) -> str:
    v = r.value
    if v is None:
        return "-"
    if kind == "pct":
        return r.pct() if with_sample else f"%{100 * v:.0f}"
    if kind == "sec":
        return f"{v:.1f} sn"
    if kind == "dist":
        return f"{v:.0f}"
    if kind == "num":
        return f"{v:.1f}"
    return f"{v:.2f}"


def _diff(me: Rate, lobby: Rate, kind: str) -> str:
    a, b = me.value, lobby.value
    if a is None or b is None:
        return ""
    if kind == "pct":
        pts = round(100 * (a - b))
        return "±0 puan" if pts == 0 else f"{pts:+d} puan"
    if kind == "sec":
        return f"{a - b:+.1f} sn"
    if b == 0:
        return ""
    return f"%{100 * (a - b) / abs(b):+.0f}"


def highlights(me: dict[str, Rate], lobby: dict[str, Rate]) -> tuple[list[str], list[str]]:
    """Lobiden belirgin iyi / kötü olan metrikler (yeterli örneklemle)."""
    found: list[tuple[float, str, bool]] = []
    for metrics in SECTIONS.values():
        for m in metrics:
            a, b = me[m.key], lobby[m.key]
            if (m.better is None or a.value is None or not b.value
                    or a.den < MIN_SAMPLE or a.num < MIN_EVENTS):
                continue
            rel = (a.value - b.value) / abs(b.value)
            if m.kind == "pct":
                if abs(a.value - b.value) < MIN_PCT_POINTS:
                    continue
            elif abs(rel) < MIN_REL_DIFF:
                continue
            good = (rel > 0) == (m.better == "high")
            text = (f"**{m.label}**: {_fmt(a, m.kind, True)} — lobi {_fmt(b, m.kind)}")
            found.append((abs(rel), text, good))
    found.sort(reverse=True)
    return [t for _, t, g in found if g][:4], [t for _, t, g in found if not g][:4]


def _faceit_section(con: duckdb.DuckDBPyConnection, faceit_id: str) -> list[str]:
    rows = con.execute(
        """
        SELECT m.map, count(*) AS n, avg(s.result::INT) AS win,
               sum(s.kills) / nullif(sum(s.deaths), 0) AS kd, avg(s.adr) AS adr,
               avg(s.hs_pct) AS hs
        FROM match_player_stats s JOIN matches m USING (match_id)
        WHERE s.player_id = ? GROUP BY m.map ORDER BY n DESC
        """,
        [faceit_id],
    ).fetchall()
    if not rows:
        return []
    total = sum(r[1] for r in rows)
    lines = [
        f"## FACEIT geneli ({total} maç, tüm haritalar)",
        "",
        "FACEIT skor tablosundan; demo gerektirmez.",
        "",
        "| Harita | Maç | Kazanma | K/D | ADR | HS% |",
        "|---|---|---|---|---|---|",
    ]
    for mp, n, win, kd, adr, hs in rows:
        lines.append(
            f"| {(mp or '?').removeprefix('de_')} | {n} | %{100 * (win or 0):.0f} | "
            f"{kd or 0:.2f} | {adr or 0:.1f} | %{hs or 0:.0f} |"
        )
    form = con.execute(
        """
        SELECT s.result FROM match_player_stats s JOIN matches m USING (match_id)
        WHERE s.player_id = ? ORDER BY m.date DESC LIMIT 10
        """,
        [faceit_id],
    ).fetchall()
    lines += ["", "Son 10 maç (yeniden eskiye): " + " ".join("G" if r[0] else "M" for r in form),
              ""]
    return lines


@dataclass
class PlayerReport:
    nickname: str
    markdown: str
    demo_matches: int
    t_role: str | None
    ct_role: str | None


def build_player_report(con: duckdb.DuckDBPyConnection, nickname: str,
                        map_name: str | None = None) -> PlayerReport:
    """Oyuncu raporunu Markdown olarak üretir."""
    row = con.execute(
        "SELECT faceit_id, steam_id, nickname FROM players WHERE lower(nickname) = lower(?)",
        [nickname],
    ).fetchone()
    if not row:
        raise LookupError(f"'{nickname}' veritabanında yok. Önce `director sync {nickname}`.")
    faceit_id, steam_id, nick = row

    d = load_demo_data(con, map_name)
    pm = player_match_metrics(d)
    mine = pm.filter(pl.col("steam_id") == steam_id)
    lobby = pm.filter(pl.col("match_id").is_in(mine["match_id"].implode())
                      & (pl.col("steam_id") != steam_id))

    scope = f" — {map_name.removeprefix('de_')}" if map_name else ""
    lines = [f"# Oyuncu raporu: {nick}{scope}", "",
             f"_Oluşturulma: {date.today():%d.%m.%Y}_", ""]

    if mine.height == 0:
        lines += ["Bu oyuncu için demo verisi olan maç yok. `director ingest-demos` ile demo ekle.",
                  ""]
        lines += _faceit_section(con, faceit_id)
        return PlayerReport(nick, "\n".join(lines), 0, None, None)

    me, lb = aggregate(mine), aggregate(lobby)
    scores = role_scores(pm, steam_id)
    t_role, ct_role = primary_roles(scores)
    maps = d.match_maps.filter(pl.col("match_id").is_in(mine["match_id"].implode()))
    map_counts = maps.group_by("map").len().sort("len", descending=True)
    first, last = maps["date"].min(), maps["date"].max()

    # --- özet
    good, bad = highlights(me, lb)
    lines += ["## Özet", ""]
    sr = side_roles(scores)
    for side in ("T", "CT"):
        roles = sr[side]
        if not roles:
            lines.append(f"- {side} rolü: belirgin değil")
            continue
        txt = f"- {side} rolü: **{roles[0].name}** (güven: {roles[0].confidence})"
        if len(roles) > 1:
            txt += f", ikincil: {roles[1].name} (güven: {roles[1].confidence})"
        lines.append(txt)
    sniper, awp = me["sniper_share"], me["awp_share"]
    if (sniper.value or 0) >= AWP_THRESHOLD / 2:
        lines.append(
            f"- Keskin nişancı payı {_fmt(sniper, 'pct')} (AWP {_fmt(awp, 'pct')}, "
            f"Scout %{100 * ((sniper.value or 0) - (awp.value or 0)):.0f}); "
            f"lobi {_fmt(lb['sniper_share'], 'pct')}"
        )
    home_all = mine.filter(pl.col("ct_home_zone").is_not_null())["ct_home_zone"]
    if len(home_all):
        top_zone, top_n = home_all.value_counts().sort("count", descending=True).row(0)
        lines.append(f"- CT'de en çok tutulan alan: **{top_zone}** "
                     f"({top_n}/{len(home_all)} maç)")
    lines.append(f"- Round kazanma {me['round_win'].pct()}, K/D {_fmt(me['kd'], 'ratio')}, "
                 f"ADR {_fmt(me['adr'], 'num')}, KAST {_fmt(me['kast'], 'pct')}")
    lines += ["", "**Lobiden belirgin iyi:**", ""]
    lines += [f"- {t}" for t in good] or ["- (yeterli örneklemle belirgin fark yok)"]
    lines += ["", "**Lobiden belirgin zayıf:**", ""]
    lines += [f"- {t}" for t in bad] or ["- (yeterli örneklemle belirgin fark yok)"]
    lines += ["", f"_Lobi = aynı {mine.height} maçtaki diğer {lobby['steam_id'].n_unique()} "
              f"oyuncu. Belirgin = yüzdelerde en az {100 * MIN_PCT_POINTS:.0f} puan, diğer "
              f"metriklerde en az %{100 * MIN_REL_DIFF:.0f} fark; en az {MIN_SAMPLE} örnek ve "
              f"{MIN_EVENTS} olay._", ""]

    # --- örneklem
    lines += [
        "## Örneklem",
        "",
        f"- Demo verisi: **{mine.height} maç, {me['rounds'].num:.0f} round** "
        f"({me['t_kpr'].den:.0f} T, {me['ct_kpr'].den:.0f} CT)",
        "- Haritalar: " + ", ".join(f"{m.removeprefix('de_')} ({n})"
                                    for m, n in map_counts.iter_rows()),
        f"- Tarih aralığı: {first:%d.%m.%Y} – {last:%d.%m.%Y}",
        "- Sadece FACEIT skor tablosuyla doğrulanmış demolar kullanıldı.",
        "",
    ]

    # --- metrik tabloları
    lines += ["## Demo metrikleri", ""]
    for section, metrics in SECTIONS.items():
        lines += [f"### {section}", "", "| Metrik | Sen | Lobi | Fark |", "|---|---|---|---|"]
        for m in metrics:
            a, b = me[m.key], lb[m.key]
            flag = " ⚠️ az örneklem" if a.value is not None and a.den < MIN_SAMPLE \
                and m.kind == "pct" else ""
            lines.append(f"| {m.label} | {_fmt(a, m.kind, True)}{flag} | {_fmt(b, m.kind)} | "
                         f"{_diff(a, b, m.kind)} |")
        lines.append("")

    # --- roller
    lines += ["## Roller", "",
              "Takım içi karşılaştırma: her maçta oyuncu kendi takımındaki 4 kişiyle "
              "sıralanır (0 = takımın en düşüğü, 1 = en yükseği), maçlar boyunca ortalanır. "
              "0,60 altı rol sinyali sayılmaz.", "",
              "| Rol | Puan | Güven | Maç | Neye bakıldı |", "|---|---|---|---|---|"]
    for r in sorted(scores.values(), key=lambda x: x.score or 0, reverse=True):
        score = "-" if r.score is None else f"{r.score:.2f}"
        lines.append(f"| {r.name} | {score} | {r.confidence} | {r.matches} | "
                     f"{ROLE_SIGNALS[r.role]} |")
    home = mine.filter(pl.col("ct_home_zone").is_not_null())["ct_home_zone"]
    if len(home):
        zone_counts = home.value_counts().sort("count", descending=True)
        lines += ["", "CT'de maç başına en çok tutulan alan: " + ", ".join(
            f"{z} ({n} maç)" for z, n in zone_counts.iter_rows())]
    lines.append("")

    # --- silahlar
    w = weapon_breakdown(d, steam_id).head(8)
    lines += ["## Silahlar", "", "| Silah | Kill | Pay | HS |", "|---|---|---|---|"]
    for weapon, k, hs, share in w.select("weapon", "kills", "hs", "share").iter_rows():
        lines.append(f"| {weapon} | {k} | %{100 * share:.0f} | {hs} |")
    lines.append("")

    # --- silah × bölge (kill + hasar + duvar arkası)
    wp_all = wp = weapon_place_breakdown(d, steam_id)
    has_dmg = "damage" in wp.columns
    # Anlamlı satırlar: en az 3 kill ya da (hasar verisi varsa) en az 300 hasar
    keep = pl.col("kills") >= 3
    if has_dmg:
        keep = keep | (pl.col("damage").fill_null(0) >= 300)
    wp = wp.filter(keep).head(12)
    v2 = [m for m in d.v2_match_ids() if m in set(mine["match_id"].to_list())]
    n_all, n_v2 = mine.height, len(v2)
    lines += ["## Silah × bölge", "",
              "Oyuncunun hangi bölgeden hangi silahla iş gördüğü. Sadece kill değil: verilen "
              "hasar ve kapı / duvar arkasından vuruşlar da. Kolon başlığındaki maç sayısına "
              "dikkat: hasar ve wallbang verisi daha az maçta var.", ""]
    head = (f"| Bölge | Silah | Kill ({n_all} maç) | Opening ({n_all}) | Bölgede ölüm ({n_all}) "
            f"| Wallbang kill ({n_v2}) |")
    sep = "|---|---|---|---|---|---|"
    if has_dmg:
        head += f" Hasar ({n_v2}) | İsabet ({n_v2}) | Duvar arkası hasar* ({n_v2}) |"
        sep += "---|---|---|"
    lines += [head, sep]
    for r in wp.iter_rows(named=True):
        line = (f"| {r['place'] or '?'} | {r['weapon']} | {r['kills']} | {r['openings']} | "
                f"{r['deaths_here'] or 0} | {r['wallbangs']} |")
        if has_dmg:
            line += (f" {r['damage'] or 0:.0f} | {r['hits'] or 0} | "
                     f"{r['wall_damage'] or 0:.0f} ({r['wall_hits'] or 0} isabet) |")
        lines.append(line)
    lines += [
        "",
        "- Wallbang kill kesin: oyun motorunun \"mermi yüzey deldi\" bilgisi.",
    ]
    if has_dmg:
        lines.append(
            "- (*) Duvar arkası hasar **tahmini**: harita geometrisiyle görüş kontrolü. "
            "Gerçek duvar arkası vuruşların ~%73'ünü yakalar, direkt vuruşların ~%3'ünü "
            "yanlışlıkla duvar arkası sayar."
        )
    if n_v2 < n_all:
        lines.append(f"- {n_all - n_v2} maç eski parser ile işlendi; wallbang ve hasar verisi "
                     "için demoları yeniden işlenmeli.")
    lines.append("")

    # --- silah maruziyeti: toplam yerine "o silahla oynanan round" başına
    lines += _exposure_section(d, steam_id, wp_all)

    # --- bölgeler
    lines += ["## Bölgeler", ""]
    for kind, title in (("death", "Ölüm yerleri"), ("kill", "Kill yerleri")):
        pb = place_breakdown(d, steam_id, kind)
        lines += [f"### {title}", ""]
        for side in ("T", "CT"):
            part = pb.filter(pl.col("side") == side).head(5)
            if part.height == 0:
                continue
            total = part["side_total"][0]
            items = []
            for place, n, traded, share in part.select("place", "n", "traded", "share").iter_rows():
                extra = f", {traded} trade edildi" if kind == "death" else ""
                items.append(f"{place or '?'} %{100 * share:.0f} ({n}{extra})")
            lines.append(f"- **{side}** ({total}): " + " · ".join(items))
        lines.append("")

    # --- ekonomi
    eco = economy_breakdown(d, steam_id)
    lines += ["## Ekonomi (takımın alım tipine göre)", "",
              "| Taraf | Alım | Round | Kazanma | Round başı kill |", "|---|---|---|---|---|"]
    for side, buy, n, wins, kills in eco.select("side", "own_buy", "rounds", "wins",
                                                "kills").iter_rows():
        lines.append(f"| {side} | {BUY_TR.get(buy, buy or '?')} | {n} | "
                     f"{Rate(wins, n).pct()} | {kills / n:.2f} |")
    lines.append("")

    lines += _faceit_section(con, faceit_id)

    lines += [
        "## Veri notları",
        "",
        "- Opening: round'un ilk kill'i. Trade: awpy tanımı (5,25 sn içinde intikam).",
        "- İlk temas: oyuncunun round'daki ilk kill'i ya da ölümü (hasar dahil değil).",
        "- Takım merkezine uzaklık: T'de 5-40. saniyeler, en az 2 takım arkadaşı hayattayken.",
        "- CT site tutma / değiştirme: 15-35. saniyede bulunulan alan (A / B / Mid); "
        "alan tanımı şimdilik sadece Dust2 için var.",
        "- Ölümlere bomba ve düşme ölümleri dahil (FACEIT ile aynı).",
        "",
    ]
    return PlayerReport(nick, "\n".join(lines), mine.height,
                        t_role.name if t_role else None, ct_role.name if ct_role else None)


def _exposure_table(ex: pl.DataFrame, with_side: bool = True) -> list[str]:
    """weapon_exposure çıktısını round başına oranlarla Markdown tabloya çevirir."""
    ex = ex.filter(pl.col("rounds") >= EXPOSURE_MIN_ROUNDS)
    if ex.height == 0:
        return ["Yeterli round yok.", ""]
    side_head, side_sep = ("Taraf | ", "---|") if with_side else ("", "")
    lines = [f"| {side_head}Ana silah | Round (full buy) | Round kazanma | Kill/R | "
             "Ana silahla kill/R | Opening/R | Wallbang kill | Hasar/R | Duvar arkası hasar*/R "
             "| Ölüm/R |",
             f"|{side_sep}---|---|---|---|---|---|---|---|---|---|"]
    for r in ex.iter_rows(named=True):
        n = r["rounds"]
        few = " (az örnek)" if n < EXPOSURE_FEW else ""
        side = f"{r['side']} | " if with_side else ""
        lines.append(
            f"| {side}{r['main_weapon']} | {n} ({r['full_rounds']}){few} | "
            f"%{100 * r['wins'] / n:.0f} | {r['kills'] / n:.2f} | {r['kills_main'] / n:.2f} | "
            f"{r['openings'] / n:.2f} | {r['wallbangs']} ({r['wallbangs'] / n:.2f}/R) | "
            f"{r['damage'] / n:.0f} | {r['wall_damage'] / n:.0f} | {r['deaths'] / n:.2f} |"
        )
    return lines + [""]


def _exposure_section(d: DemoData, steam_id: str, wp: pl.DataFrame) -> list[str]:
    """Ana silaha göre round başına performans; genel + keskin nişancı bölgesi."""
    ex = weapon_exposure(d, steam_id)
    if ex.height == 0:
        return []
    lines = ["## Silah maruziyeti (round başına)", "",
             "Toplam kill, silahın kaç round kullanıldığını yok sayar. Burada her round'a "
             "oyuncunun **ana silahı** atanır (konum kayıtlarında en çok elde tutulan birincil "
             "silah; hiç yoksa pistol) ve değerler o silahla oynanan round sayısına bölünür. "
             "Parantezdeki sayı, o round'lardan kaçında takımın full buy yaptığıdır: force "
             "round'larda takımın donanımı da zayıftır, kazanma oranını düşürür.", ""]
    lines += _exposure_table(ex)

    # Keskin nişancı kill'lerinin en çok geldiği bölge: hem tüm round'lar hem full buy
    sn = (wp.filter(pl.col("weapon").is_in(SNIPERS) & pl.col("place").is_not_null())
          .group_by("place").agg(pl.col("kills").sum()).sort("kills", descending=True))
    if sn.height and sn["kills"][0] >= MIN_EVENTS:
        place = sn["place"][0]
        for buy, title in ((None, "tüm round'lar"), ("full", "sadece full buy")):
            part = weapon_exposure(d, steam_id, place, buy).filter(
                pl.col("main_weapon").is_in(SNIPERS))
            lines += [f"### {place}: AWP ve Scout, {title}", ""]
            lines += _exposure_table(part)
        lines.append(f"- {place} tablolarında sadece oyuncunun o bölgede bulunduğu round'lar var; "
                     "kill, hasar ve ölüm de sadece o bölgeden sayılır.")
    lines += [
        "- Ana silah round boyunca en çok tutulan silahtır: AWP ile başlayıp düşürülen ya da "
        "sonradan alınan silah round'u o silaha saymayabilir.",
        "- (*) Duvar arkası hasar **tahmini** (bkz. Silah × bölge).",
        "",
    ]
    return lines
