"""Arena taktik modu (analysis/replay.py): tek örnek akış, olasılık alanı yok, tohum tekrarı, rota."""

from __future__ import annotations

import time

import pytest

from squadmind.analysis import replay as rp
from squadmind.analysis import simulate as sm

try:
    from awpy import data
    HAS_MAP = data.mesh_path("de_dust2").exists()
except Exception:  # noqa: BLE001
    HAS_MAP = False
needs_map = pytest.mark.skipif(not HAS_MAP, reason="Dust2 geometrisi yerelde yok")

A_LONG = ((350.0, -61.0), (815.0, 758.0), (1560.0, 1380.0), (1097.0, 2760.0))
BANNED = ("p", "win", "prob", "probability", "lo", "hi", "p_t", "score", "winner", "kazanan")


def slots(elo_t=1700.0, elo_c=1700.0):
    t = [rp.Slot(f"T{i}", elo_t, "rifle", A_LONG, delay=0.35 * i, plant=(i == 4)) for i in range(5)]
    ct = [rp.Slot(f"C{i}", elo_c, "rifle", (h,)) for i, h in enumerate(rp.DEFAULT_CT_HOLDS)]
    return t, ct


def keys_of(obj):
    """Sözlüklerin her derinlikteki anahtarları."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from keys_of(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from keys_of(v)


@needs_map
def test_flow_has_no_probability_or_winner_fields():
    scn, _ = rp.build_scenario(*slots())
    flow = rp.replay(rp.prepare_cached(scn), seed=3)
    flow["label"] = rp.FLOW_LABEL
    ks = set(keys_of(flow))
    assert not ks & set(BANNED), ks & set(BANNED)
    assert not any("prob" in k.lower() or "win" in k.lower() for k in ks)
    assert {"dt", "steps", "end_t", "units", "events"} <= ks
    # Olay türleri yalnız betimleyici; round sonu için kazanan bildiren tür yok
    assert {e["kind"] for e in flow["events"]} <= {"los", "duel", "plant"}


@needs_map
def test_same_seed_same_flow_other_seed_differs():
    scn, _ = rp.build_scenario(*slots())
    prep = rp.prepare_cached(scn)
    a, b = rp.replay(prep, seed=11), rp.replay(prep, seed=11)
    assert a == b
    assert any(rp.replay(prep, seed=s) != a for s in range(12, 30))


@needs_map
def test_timeline_shape_and_dead_tokens_stay_put():
    scn, _ = rp.build_scenario(*slots())
    flow = rp.replay(rp.prepare_cached(scn), seed=5)
    assert len(flow["units"]) == 10 and flow["dt"] == 0.25
    for u in flow["units"]:
        assert len(u["xy"]) == flow["steps"]
        if u["died"] is not None:                       # ölen jeton olduğu yerde kalır
            k = int(round(u["died"] / flow["dt"]))
            assert all(p == u["xy"][k] for p in u["xy"][k:])
    duels = [e for e in flow["events"] if e["kind"] == "duel"]
    assert sum(u["died"] is not None for u in flow["units"]) == len(duels)
    assert [e["t"] for e in flow["events"]] == sorted(e["t"] for e in flow["events"])


@needs_map
def test_route_snaps_to_walkable_area_and_rejects_unreachable():
    snapped, path = rp.resolve_route("de_dust2", rp.T_SPAWN, [(350.0, -61.0), (1097.0, 2760.0)])
    assert len(snapped) == 2 and len(path) >= 3
    x, y, _ = rp.snap("de_dust2", 50000.0, 50000.0)      # haritanın dışı → en yakın alan merkezine
    assert abs(x) < 6000 and abs(y) < 6000


@needs_map
def test_one_replay_is_fast_when_visibility_is_cached():
    scn, _ = rp.build_scenario(*slots())
    prep = rp.prepare_cached(scn)
    t0 = time.perf_counter()
    rp.replay(prep, seed=1)
    assert time.perf_counter() - t0 < 0.3
    assert rp.prepare_cached(scn) is prep                 # aynı kurulum: görüş tablosu yeniden hesaplanmaz


def test_replay_without_map_uses_given_visibility():
    unit = lambda n, x, side_cls="rifle": sm.Unit(n, 1500.0, sm.walk([(x, 0.0)]), side_cls)   # noqa: E731
    scn = sm.Scenario("de_dust2", (unit("t", 0.0),), (unit("c", 500.0),), horizon=1.0)
    flow = rp.replay(sm.prepare(scn, lambda a, b: True), seed=2)
    kinds = [e["kind"] for e in flow["events"]]
    assert kinds == ["los", "duel"] and flow["end_t"] == 0.0
    assert flow["units"][0]["died"] is not None or flow["units"][1]["died"] is not None


def test_label_text_is_fixed():
    assert rp.FLOW_LABEL == "Örnek akış: tahmin değil, betimleme. Sınamayı geçmedi."
