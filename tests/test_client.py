"""FaceitClient testleri: cache, sayfalama, backoff, hata yönetimi."""

from __future__ import annotations

import os
import time

import httpx
import pytest

from squadmind.faceit.client import FaceitAPIError, FaceitNotFound


def test_player_by_nickname_sends_auth_and_params(make_client, recorder):
    client = make_client()
    player = client.get_player_by_nickname("TestNick")

    assert player["player_id"] == "p-self"
    req = recorder.requests[0]
    assert req.headers["Authorization"] == "Bearer test-key"
    assert req.url.params["nickname"] == "TestNick"
    assert req.url.params["game"] == "cs2"


def test_second_call_is_served_from_cache(make_client, recorder):
    client = make_client()
    client.get_player_by_nickname("TestNick")
    client.get_player_by_nickname("TestNick")

    # İkinci çağrı ağa çıkmamalı
    assert len(recorder.requests) == 1
    assert list(client.cache_dir.glob("*.json"))


def test_expired_profile_cache_is_refetched(make_client, recorder):
    client = make_client()
    client.get_player_by_nickname("TestNick")
    # Cache dosyasını 2 saat öncesine çek → 1 saatlik TTL dolmuş olur
    file = client.cache_path("/players", {"nickname": "TestNick", "game": "cs2"})
    old = time.time() - 7200
    os.utime(file, (old, old))

    client.get_player_by_nickname("TestNick")
    assert len(recorder.requests) == 2


def test_match_details_cache_is_permanent(make_client, recorder):
    client = make_client()
    client.get_match_details("m-1")
    file = client.cache_path("/matches/m-1")
    old = time.time() - 30 * 24 * 3600
    os.utime(file, (old, old))

    client.get_match_details("m-1")
    assert len(recorder.requests) == 1


def test_unfinished_match_is_not_cached(make_client, recorder):
    client = make_client()
    client.get_match_details("m-0")  # CANCELLED
    client.get_match_details("m-0")
    assert len(recorder.requests) == 2


def test_history_paginates_until_limit(make_client, recorder):
    client = make_client()
    items = client.get_player_history("p-self", limit=3, page_size=2)

    assert [i["match_id"] for i in items] == ["m-2", "m-1", "m-0"]
    offsets = [int(r.url.params["offset"]) for r in recorder.requests]
    assert offsets == [0, 2]


def test_history_stops_when_no_more_items(make_client, recorder):
    client = make_client()
    items = client.get_player_history("p-self", limit=50)
    # Fixture'da sadece 3 maç var; tek istek yeterli
    assert len(items) == 3
    assert len(recorder.requests) == 1


def test_429_triggers_exponential_backoff(make_client, recorder):
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"player_id": "p-self"})

    client = make_client(flaky, backoff_base=1.0)
    data = client.get_player_by_nickname("TestNick")

    assert data["player_id"] == "p-self"
    assert recorder.sleeps == [1.0, 2.0, 4.0]


def test_429_respects_retry_after_header(make_client, recorder):
    calls = {"n": 0}

    def limited(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    client = make_client(limited)
    client.get_match_stats("m-x")
    assert recorder.sleeps == [7.0]


def test_gives_up_after_max_retries(make_client, recorder):
    client = make_client(lambda r: httpx.Response(429), max_retries=2)
    with pytest.raises(FaceitAPIError) as exc:
        client.get_player_by_nickname("TestNick")
    assert exc.value.status_code == 429
    assert len(recorder.requests) == 3  # ilk deneme + 2 tekrar


def test_404_raises_not_found_and_is_not_cached(make_client, recorder):
    client = make_client()
    with pytest.raises(FaceitNotFound):
        client.get_player_by_nickname("Yok")
    assert not list(client.cache_dir.glob("*.json"))


def test_missing_api_key_raises(monkeypatch, tmp_path):
    from squadmind.faceit.client import FaceitClient

    monkeypatch.delenv("FACEIT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FACEIT_API_KEY"):
        FaceitClient(cache_dir=tmp_path)
