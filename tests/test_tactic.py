"""Taktik tahtası sayfası (viz/tactic.py + /taktik) testleri.

Strat dosyası ve sekme seçimi her zaman; navmesh isteyen çizim testi sadece Dust2
navmesh'i yerelde indirilmişse çalışır.
"""

from __future__ import annotations

import json

import pytest

from squadmind.viz.tactic import STRATS_DIR, Strat, build_board, find_tab, load_strats, stat_line
from squadmind.viz.board import Spot


def test_repo_strat_is_complete():
    strats = load_strats(STRATS_DIR, "de_dust2")
    assert strats, "plans/strats içinde Dust2 stratı yok"
    for s in strats:
        labels = {p["label"] for p in s.raw["players"]}
        assert len(labels) == 5
        # Her utility'nin atanı kadroda, her oyuncunun en az iki anahtar karesi var
        assert all(u["thrower"] in labels for u in s.raw["utility"])
        assert all(len(p["keys"]) >= 2 for p in s.raw["players"])
        # Zaman çizelgesi sıralı: "şu an" kartı sırayla ilerler
        for p in s.raw["players"]:
            ts = [t for t, *_ in p["timeline"]]
            assert ts == sorted(ts)
        assert len({t["id"] for t in s.tabs}) == len(s.tabs)


def test_load_strats_filters_map_and_find_tab_falls_back(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(
        {"id": "a", "map": "de_dust2", "tabs": [{"id": "r1", "label": "R1"}, {"id": "r2", "label": "R2"}]}),
        encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"id": "b", "map": "de_mirage", "tabs": []}),
                                     encoding="utf-8")
    strats = load_strats(tmp_path, "de_dust2")
    assert [s.id for s in strats] == ["a"]
    assert find_tab(strats, "r2")[1]["id"] == "r2"
    assert find_tab(strats, "yok")[1]["id"] == "r1"     # bilinmeyen sekme → ilk sekme
    assert find_tab([], "r1") is None


def test_stat_line_reports_sample_and_missing_data():
    stats = {"pistol": {"won": 10, "n": 18}, "r2_after_loss": {"won": 0, "n": 0}}
    assert stat_line({"stat": "pistol"}, stats, "T") == "T pistol round'u: 10/18 kazanıldı (%56)"
    assert stat_line({"stat": "r2_after_loss"}, stats, "T").endswith("veri yok")
    assert stat_line({}, stats, "T") == ""


def test_tactic_page_requires_login_and_handles_missing_strats(tmp_path, make_client):
    from fastapi.testclient import TestClient

    from squadmind.web.app import WebSettings, create_app
    from squadmind.web.store import Store
    from squadmind.web.worker import Worker

    store = Store(tmp_path / "web.duckdb")
    worker = Worker(store, tmp_path / "p", tmp_path / "t", client_factory=make_client)
    worker.submit = lambda job: None
    settings = WebSettings(invite_code="davet", secret="s", required_demos=10, team_owner=None,
                           map_name="de_dust2", uploads_dir=tmp_path / "u",
                           strats_dir=tmp_path / "bos")
    client = TestClient(create_app(settings, store, worker, client_factory=make_client))
    try:
        r = client.get("/taktik", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        client.post("/katil", data={"nickname": "TestNick", "davet": "davet"})
        assert "henüz strat yok" in client.get("/taktik").text
    finally:
        store.close()


def _nav_available() -> bool:
    try:
        from awpy import data
        return data.nav_path("de_dust2").exists()
    except Exception:
        return False


@pytest.mark.skipif(not _nav_available(), reason="Dust2 navmesh yerelde yok")
def test_build_board_projects_players_utility_and_spots():
    strat = load_strats(STRATS_DIR, "de_dust2")[0]
    bg, data = build_board(strat, [Spot((900, 2200), 0.3, "%30")])
    assert bg.startswith("<svg") and "<style" not in bg
    n = int(round(data["duration"] / data["dt"])) + 1
    assert all(len(p["frames"]) == n for p in data["players"])
    # Tüm kareler tahtanın içinde
    for p in data["players"]:
        assert all(0 <= x <= data["w"] and 0 <= y <= data["h"] for x, y in p["frames"])
    assert {u["kind"] for u in data["utility"]} >= {"smoke", "flash"}
    assert data["spots"][0]["label"] == "%30"
    assert set(data["decisions"]) == {p["label"] for p in data["players"]}


def test_fill_evidence_formats_and_flags(monkeypatch):
    from squadmind.analysis.player_metrics import Rate
    from squadmind.viz import tactic

    rates = {"a": Rate(9, 9), "b": Rate(17, 25), "z": Rate(0, 0)}
    monkeypatch.setattr(tactic, "measure", lambda item, d, sid: rates[item.label])
    m = lambda fmt: {"kind": "x", "fmt": fmt}  # noqa: E731
    assert tactic.fill_evidence("{a} ({a.n})", {"a": m("record")}, None, "s") == "9-0 (9) Az örnek."
    assert tactic.fill_evidence("{b}", {"b": m("pct")}, None, "s") == "%68"
    assert tactic.fill_evidence("{b}", {"b": m("ratio")}, None, "s") == "0,68"
    # Örneği olmayan metrik: sayı uydurulmaz, dal veri yok olur
    assert tactic.fill_evidence("{z}", {"z": m("pct")}, None, "s") == tactic.NO_DATA
    assert tactic.fill_evidence("Standart görev.", {}, None, "s") == "Standart görev."
