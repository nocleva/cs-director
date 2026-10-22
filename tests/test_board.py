"""Taktik tahtası (viz/board.py) testleri.

Saf yardımcılar her zaman; navmesh isteyen çizim testleri sadece Dust2 navmesh'i yerelde
indirilmişse çalışır (internetten indirme yapılmaz).
"""

from __future__ import annotations

import pytest

from squadmind.viz.board import (
    Board,
    Player,
    Spot,
    Utility,
    _offset,
    _smooth_path,
    merge_spots,
    render_board,
)


def test_merge_spots_keeps_strongest_and_drops_neighbours():
    spots = [Spot((0, 0), 0.30), Spot((100, 0), 0.25), Spot((1000, 0), 0.20), Spot((1050, 0), 0.10)]
    kept = merge_spots(spots, min_dist=280, keep=6)
    assert [s.share for s in kept] == [0.30, 0.20]


def test_offset_shifts_perpendicular():
    # Sağa giden düz çizgi: yanal kaydırma y ekseninde olur
    assert _offset([(0, 0), (10, 0)], 5) == [(0.0, 5.0), (10.0, 5.0)]
    assert _offset([(0, 0), (10, 0)], 0) == [(0, 0), (10, 0)]


def test_smooth_path_passes_through_points():
    d = _smooth_path([(0, 0), (10, 0), (10, 10)])
    assert d.startswith("M0.0,0.0") and d.endswith("10.0,10.0")
    assert d.count("C") == 2


def _nav_available() -> bool:
    try:
        from awpy import data
        return data.nav_path("de_dust2").exists()
    except Exception:
        return False


@pytest.mark.skipif(not _nav_available(), reason="Dust2 navmesh yerelde yok")
def test_render_board_draws_map_routes_and_utility():
    board = Board(
        map_name="de_dust2",
        places={"Long A": (1500, 1500)},
        players=[Player("1", "Test<Oyuncu>", (-636, -713, 101), [(350, -61, 5), (1330, 1760, 0)])],
        utility=[Utility("smoke", (-300, 1400, -60), "0:06 XBOX", thrower=(-313, 122, 7))],
        spots=[Spot((900, 2200), 0.3, "%30")],
        target_places=[(1097, 2649)],
    )
    svg = render_board(board)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "<style" not in svg              # web'e <img> olarak konabilsin
    assert "LONG A" in svg and "0:06 XBOX" in svg and "%30" in svg
    assert "Test&lt;Oyuncu&gt;" in svg      # dış metin kaçışlanır
    assert 'marker-end="url(#arrT)"' in svg
