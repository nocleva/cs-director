"""Taktik oynatma (viz/playback.py) testleri: zamanlama ve yol üzerinde konum."""

from __future__ import annotations

import pytest

from squadmind.viz.playback import Actor, Keyframe, _at_distance, _cumulative, actor_frames


def test_at_distance_along_polyline():
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    acc = _cumulative(pts)
    assert acc == [0.0, 10.0, 20.0]
    assert _at_distance(pts, acc, 5) == (5.0, 0.0)
    assert _at_distance(pts, acc, 15) == (10.0, 5.0)
    assert _at_distance(pts, acc, 99) == (10.0, 10.0)   # yolun sonunda kalır
    assert _at_distance(pts, acc, -1) == (0.0, 0.0)


def test_frames_hold_before_first_and_after_last_key():
    # Aynı noktada iki anahtar kare = bekleme; navmesh gerekmez
    a = Actor("1", "Test", keys=[Keyframe(2, (5, 5, 0)), Keyframe(4, (5, 5, 0))])
    frames = actor_frames("de_dust2", a, duration=6, dt=1.0)
    assert len(frames) == 7
    assert set(frames) == {(5, 5)}


def _nav_available() -> bool:
    try:
        from awpy import data
        return data.nav_path("de_dust2").exists()
    except Exception:
        return False


@pytest.mark.skipif(not _nav_available(), reason="Dust2 navmesh yerelde yok")
def test_frames_move_monotonically_toward_target():
    import math

    start, end = (-636, -713, 101), (350, -61, 5)
    a = Actor("1", "Test", keys=[Keyframe(0, start), Keyframe(5, end)])
    frames = actor_frames("de_dust2", a, duration=6, dt=0.5)
    d = [math.dist(f, end[:2]) for f in frames]
    assert d[0] > d[5] > d[-1]
    assert frames[-1] == pytest.approx(end[:2], abs=1)
