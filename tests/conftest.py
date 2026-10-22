"""Ortak test yardımcıları: kaydedilmiş JSON fixture'larıyla sahte FACEIT API."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from director.db import connect, init_db
from director.faceit.client import FaceitClient

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fake_faceit_handler(request: httpx.Request) -> httpx.Response:
    """URL yolunu fixture dosyasına eşler; gerçek API'ye asla gidilmez."""
    path = request.url.path.removeprefix("/data/v4")
    params = request.url.params

    if path == "/players" and params.get("nickname") == "TestNick":
        return httpx.Response(200, json=load_fixture("player.json"))
    if path == "/players/p-self/history":
        data = load_fixture("history.json")
        # offset/limit'e göre sayfala (gerçek API davranışı)
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 20))
        data["items"] = data["items"][offset : offset + limit]
        return httpx.Response(200, json=data)
    if path.startswith("/matches/"):
        parts = path.strip("/").split("/")
        match_id = parts[1]
        name = f"stats_{match_id}.json" if len(parts) == 3 else f"match_{match_id}.json"
        if (FIXTURES / name).exists():
            return httpx.Response(200, json=load_fixture(name))
    return httpx.Response(404, json={"errors": [{"message": "not found"}]})


class Recorder:
    """İstekleri ve uyku sürelerini kaydeder."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def make_client(tmp_path: Path, recorder: Recorder) -> Callable[..., FaceitClient]:
    """Verilen handler ile, geçici cache klasörlü test istemcisi üretir."""

    def _make(
        handler: Callable[[httpx.Request], httpx.Response] = fake_faceit_handler,
        **kwargs: Any,
    ) -> FaceitClient:
        def recording(request: httpx.Request) -> httpx.Response:
            recorder.requests.append(request)
            return handler(request)

        kwargs.setdefault("min_interval", 0)
        return FaceitClient(
            api_key="test-key",
            cache_dir=tmp_path / "cache",
            transport=httpx.MockTransport(recording),
            sleep=recorder.sleeps.append,
            **kwargs,
        )

    return _make


@pytest.fixture
def con():
    """Her test için temiz, bellek içi DuckDB."""
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def tiny_demos_allowed(monkeypatch):
    """Testlerdeki sahte demolar birkaç bayt: boyut alt sınırını kaldır.

    Boyut kuralının kendisi test_demo_guard.py'de gerçek sınırla test edilir.
    """
    import director.ingest.demos as demos

    monkeypatch.setattr(demos, "MIN_DEMO_BYTES", 1)
