"""Öneri takibi (Faz 2b): bir planın kurallarına bağlı metrikleri plan öncesi / sonrası ölçer.

Plan bir JSON dosyasıdır (`plans/*.json`): kimin takımı, hangi harita, plan tarihi ve takip
edilecek maddeler. Her madde bir metrik türü (`kind`) + parametreleridir; kod oyuncu adı
bilmez, adlar plandan gelir. Böylece yeni plan / takım kod değiştirmeden takip edilir.

Metrik türleri (hepsi Rate döner: pay / payda, örneklem görünsün diye):
- late_deaths_at_place: oyuncunun `place`'te `after` sn'den sonraki `side` ölümleri / `side` round
- traded_at_place: oyuncunun `place`'teki `side` ölümlerinden trade edilenler / ölümler
- isolated_death: oyuncunun (`side` verilirse o taraftaki) ölümlerinden yalnız olanlar / ölçülenler
- isolated_traded: oyuncunun `place`'teki yalnız `side` ölümlerinden trade edilenler / yalnız ölümler
- opening_record: oyuncunun `side` opening düellosu (`place` verilirse orada) kazanılan / toplam
- traded_by: `player`'ın ölümlerinden `by`'ın 5 sn içinde trade ettiği / `player`'ın ölümleri
- flash_at_place: oyuncunun `place`'e düşen `side` flash'larında kör edilen rakip / atış
- utility_damage: oyuncunun `side` utility hasarı / `side` round
- enemy_at_place: takım `side`'dayken rakibin `window` sn'de `place`'te görüldüğü (round, oyuncu) / round
- pattern_win: takımın `pattern` desenli T round'larından kazanılan / round (`site` ve alım tipi
  `buy` verilirse onlarla süzülür)
- spot_share: oyuncunun `side` round'larından `spot` alanında / koridorunda oynadıkları
- spot_net: `spot`'ta (kill − ölüm) / round
- opening_attempts: oyuncunun `side` opening düellosu (kill + ölüm) / `side` round
- team_traded_death: takımın ölümlerinden trade edilenler / ölümler
- retake_win: rakip plant ettikten sonra CT'nin kazandığı round / plant
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import polars as pl

from squadmind.analysis.player_metrics import DemoData, Rate, death_isolation, player_rounds
from squadmind.analysis.team_management import player_spots, trade_pairs
from squadmind.analysis.team_tactics import t_round_patterns, team_rounds

# "Sonra" tarafında bu kadar maçtan azı varsa durum "veri bekleniyor"
MIN_AFTER_MATCHES = 3


@dataclass
class TrackItem:
    label: str          # raporda görünen ad
    rule: str           # plandaki kural (ne bekleniyor)
    kind: str           # metrik türü (modül açıklamasına bak)
    direction: str      # "up" (yükselmeli) / "down" (düşmeli)
    params: dict = field(default_factory=dict)  # player, side, place, spot, after
    fmt: str = "pct"    # pct / ratio


@dataclass
class Plan:
    name: str
    nickname: str       # takımın sahibi (çekirdek kadro bu oyuncuya göre)
    map_name: str | None
    since: date         # plan tarihi: bu günden ÖNCEKİ maçlar "önce", bu gün ve sonrası "sonra"
    items: list[TrackItem]


def load_plan(path: Path) -> Plan:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = [TrackItem(label=i["label"], rule=i["rule"], kind=i["kind"],
                       direction=i["direction"], params=i.get("params", {}),
                       fmt=i.get("fmt", "pct")) for i in raw["items"]]
    return Plan(name=raw["name"], nickname=raw["nickname"], map_name=raw.get("map"),
                since=date.fromisoformat(raw["since"]), items=items)


def _steam_id(d: DemoData, nickname: str) -> str | None:
    hit = d.roster.filter(pl.col("nickname").str.to_lowercase() == nickname.lower())
    return hit["steam_id"][0] if hit.height else None


def _player_side_rounds(d: DemoData, sid: str, side: str) -> int:
    return player_rounds(d).filter((pl.col("steam_id") == sid) & (pl.col("side") == side)).height


def measure(item: TrackItem, d: DemoData, owner_sid: str) -> Rate:
    """Bir takip maddesinin değeri (verilen maç kümesinde)."""
    p = item.params
    sid = _steam_id(d, p["player"]) if "player" in p else owner_sid
    if sid is None:  # oyuncu bu maçlarda yok
        return Rate(0, 0)
    side = p.get("side")
    k = d.kills
    if item.kind == "late_deaths_at_place":
        n = k.filter((pl.col("victim") == sid) & (pl.col("victim_side") == side)
                     & (pl.col("victim_place") == p["place"])
                     & (pl.col("time_in_round") > p.get("after", 0))).height
        return Rate(n, _player_side_rounds(d, sid, side))
    if item.kind == "traded_at_place":
        dd = k.filter((pl.col("victim") == sid) & (pl.col("victim_side") == side)
                      & (pl.col("victim_place") == p["place"]))
        return Rate(int(dd["is_traded"].sum()), dd.height)
    if item.kind in ("isolated_death", "isolated_traded"):
        iso = death_isolation(d).filter((pl.col("victim") == sid)
                                        & ((pl.col("victim_side") == side) if side else pl.lit(True))
                                        & pl.col("isolated").is_not_null())
        if item.kind == "isolated_traded":
            iso = iso.filter(pl.col("isolated") & (pl.col("victim_place") == p["place"]))
            return Rate(int(iso["is_traded"].sum()), iso.height)
        return Rate(int(iso["isolated"].sum()), iso.height)
    if item.kind == "opening_record":
        op = k.filter(pl.col("is_opening"))
        won = op.filter((pl.col("attacker") == sid) & (pl.col("attacker_side") == side))
        lost = op.filter((pl.col("victim") == sid) & (pl.col("victim_side") == side))
        if "place" in p:  # düello oyuncunun o an bulunduğu yerde
            places = p["place"] if isinstance(p["place"], list) else [p["place"]]
            won = won.filter(pl.col("attacker_place").is_in(places))
            lost = lost.filter(pl.col("victim_place").is_in(places))
        return Rate(won.height, won.height + lost.height)
    if item.kind == "traded_by":
        by = _steam_id(d, p["by"])
        pairs = trade_pairs(d, owner_sid)
        hit = pairs.filter((pl.col("traded").str.to_lowercase() == p["player"].lower())
                           & (pl.col("trader").str.to_lowercase() == p["by"].lower()))
        deaths = k.filter((pl.col("victim") == sid)
                          & (pl.col("attacker_side") != pl.col("victim_side"))).height
        return Rate(int(hit["n"].sum()) if by else 0, deaths)
    if item.kind == "flash_at_place":
        fl = d.utility.filter((pl.col("thrower") == sid) & (pl.col("thrower_side") == side)
                              & (pl.col("type") == "flash") & (pl.col("land_place") == p["place"]))
        return Rate(int(fl["flashed_enemies"].fill_null(0).sum()), fl.height)
    if item.kind == "utility_damage":
        ut = d.utility.filter((pl.col("thrower") == sid) & (pl.col("thrower_side") == side))
        return Rate(float(ut["damage"].fill_null(0).sum()), _player_side_rounds(d, sid, side))
    if item.kind == "enemy_at_place":
        tr = team_rounds(d, owner_sid).filter(pl.col("side") == side).select("match_id", "round_no")
        lo, hi = p.get("window", (15, 25))
        seen = (d.positions.cast({"round_no": pl.Int64}).join(tr, on=["match_id", "round_no"])
                .filter((pl.col("side") != side) & (pl.col("place_name") == p["place"])
                        & pl.col("time_in_round").is_between(lo, hi))
                .select("match_id", "round_no", "player").unique())
        return Rate(seen.height, tr.height)
    if item.kind == "pattern_win":
        tp = t_round_patterns(d, owner_sid).filter(pl.col("pattern") == p["pattern"])
        if "site" in p:
            tp = tp.filter(pl.col("target_site") == p["site"])
        if "buy" in p:
            tp = tp.filter(pl.col("own_buy") == p["buy"])
        return Rate(int(tp["won"].sum()), tp.height)
    if item.kind in ("spot_share", "spot_net"):
        sp = player_spots(d, owner_sid)
        name = p["player"]
        mine = sp.filter((pl.col("side") == side)
                         & (pl.col("player").str.to_lowercase() == name.lower()))
        at = mine.filter(pl.col("spot") == p["spot"])
        n_at = int(at["rounds"].sum())
        if item.kind == "spot_share":
            return Rate(n_at, int(mine["rounds"].sum()))
        net = int(at["kills"].sum()) - int(at["deaths"].sum())
        return Rate(net, n_at)
    if item.kind == "opening_attempts":
        ok = k.filter((pl.col("attacker") == sid) & (pl.col("attacker_side") == side)
                      & pl.col("is_opening")).height
        od = k.filter((pl.col("victim") == sid) & (pl.col("victim_side") == side)
                      & pl.col("is_opening")).height
        return Rate(ok + od, _player_side_rounds(d, sid, side))
    if item.kind == "team_traded_death":
        mine = d.roster.filter(pl.col("steam_id") == owner_sid).select("match_id", "faction")
        team = d.roster.join(mine, on=["match_id", "faction"], how="semi")
        dd = k.join(team.select("match_id", pl.col("steam_id").alias("victim")),
                    on=["match_id", "victim"], how="semi")
        return Rate(int(dd["is_traded"].sum()), dd.height)
    if item.kind == "retake_win":
        tr = team_rounds(d, owner_sid).filter((pl.col("side") == "CT")
                                              & pl.col("bomb_site").is_not_null())
        if "site" in p:
            tr = tr.filter(pl.col("bomb_site") == p["site"])
        return Rate(int(tr["won"].sum()), tr.height)
    raise ValueError(f"bilinmeyen takip türü: {item.kind}")


@dataclass
class TrackRow:
    item: TrackItem
    before: Rate
    after: Rate
    status: str


def track(d: DemoData, plan: Plan) -> tuple[list[TrackRow], int, int]:
    """Plan maddelerini önce / sonra ölçer. Döner: satırlar, önceki maç sayısı, sonraki."""
    owner = _steam_id(d, plan.nickname)
    if owner is None:
        raise LookupError(f"'{plan.nickname}' bu maçlarda yok.")
    mm = d.match_maps.filter(pl.col("match_id").is_in(
        d.roster.filter(pl.col("steam_id") == owner)["match_id"].implode()))
    since = datetime.combine(plan.since, datetime.min.time())
    before_ids = mm.filter(pl.col("date") < since)["match_id"].to_list()
    after_ids = mm.filter(pl.col("date") >= since)["match_id"].to_list()
    d_before, d_after = d.subset(before_ids), d.subset(after_ids)
    rows = []
    for item in plan.items:
        b, a = measure(item, d_before, owner), measure(item, d_after, owner)
        rows.append(TrackRow(item, b, a, _status(item, b, a, len(after_ids))))
    return rows, len(before_ids), len(after_ids)


def _status(item: TrackItem, before: Rate, after: Rate, after_matches: int) -> str:
    if after_matches < MIN_AFTER_MATCHES or after.value is None or before.value is None:
        return "veri bekleniyor"
    diff = after.value - before.value
    # Küçük oynamaları "değişim yok" say: yüzdelerde 3 puan, oranlarda %10
    small = abs(diff) < 0.03 if item.fmt == "pct" else abs(diff) < 0.10 * max(abs(before.value), 1e-9)
    if small:
        return "değişim yok"
    good = (diff > 0) == (item.direction == "up")
    return "iyileşti" if good else "kötüleşti"
