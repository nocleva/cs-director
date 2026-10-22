"""`squadmind track <plan.json>`: planın kurallarına bağlı metrikler, plan öncesi / sonrası."""

from __future__ import annotations

from datetime import date

import duckdb

from squadmind.analysis.player_metrics import Rate, load_demo_data
from squadmind.analysis.tracking import MIN_AFTER_MATCHES, Plan, track

DIRECTION_TR = {"up": "yükselmeli", "down": "düşmeli"}


def _fmt(r: Rate, kind: str) -> str:
    v = r.value
    if v is None:
        return "-"
    if kind == "pct":
        return f"%{100 * v:.0f} ({r.num:.0f}/{r.den:.0f})"
    return f"{v:.2f} ({r.num:.0f}/{r.den:.0f})"


def build_track_report(con: duckdb.DuckDBPyConnection, plan: Plan) -> str:
    """Takip raporunu Markdown olarak üretir."""
    d = load_demo_data(con, plan.map_name)
    rows, n_before, n_after = track(d, plan)
    scope = plan.map_name.removeprefix("de_") if plan.map_name else "tüm haritalar"
    lines = [
        f"# Öneri takibi: {plan.name}", "",
        f"_Oluşturulma: {date.today():%d.%m.%Y}_", "",
        f"Plan tarihi **{plan.since:%d.%m.%Y}** ({scope}), takım: {plan.nickname}. Önce: bu "
        f"tarihten önceki **{n_before} maç**; sonra: bu tarih ve sonrası **{n_after} maç**. "
        f"Sonra tarafında en az {MIN_AFTER_MATCHES} maç olmadan durum yazılmaz. Yeni demolar "
        "`squadmind watch` ile eklendikçe tablo dolar.", "",
        "| Takip | Plandaki kural | Hedef | Önce | Sonra | Durum |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        it = r.item
        lines.append(f"| {it.label} | {it.rule} | {DIRECTION_TR[it.direction]} | "
                     f"{_fmt(r.before, it.fmt)} | {_fmt(r.after, it.fmt)} | {r.status} |")
    lines += ["", "- Durum: 3 puandan (oranlarda %10'dan) küçük fark \"değişim yok\" sayılır. "
              "Az maçla gelen değişim tesadüf olabilir; 5-10 maçtan sonra yorumla.", ""]
    return "\n".join(lines)
