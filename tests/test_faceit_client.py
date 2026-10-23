"""FACEIT istemcisi: Downloads API (imzalı demo linki) testleri."""

import json

import httpx
import pytest

from squadmind.faceit.client import FaceitAPIError, FaceitClient


# --- Downloads API (imzalı demo linki) -----------------------------------------------------

def test_get_demo_download_url_posts_resource_and_returns_signed_url(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["url"] = request.method, str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"payload": {"download_url": "https://cdn.example/x.dem.zst?sig=1"}})

    client = FaceitClient(api_key="k", cache_dir=tmp_path, transport=httpx.MockTransport(handler),
                          sleep=lambda s: None)
    url = client.get_demo_download_url("https://demos.example/1-abc.dem.zst")
    assert url == "https://cdn.example/x.dem.zst?sig=1"
    assert seen["method"] == "POST" and seen["url"].endswith("/download/v2/demos/download")
    assert seen["body"] == {"resource_url": "https://demos.example/1-abc.dem.zst"}


def test_get_demo_download_url_scope_error_is_raised(tmp_path):
    client = FaceitClient(api_key="k", cache_dir=tmp_path, sleep=lambda s: None,
                          transport=httpx.MockTransport(lambda r: httpx.Response(
                              403, json={"errors": [{"message": "no valid scope provided"}]})))
    with pytest.raises(FaceitAPIError) as e:
        client.get_demo_download_url("https://demos.example/1-abc.dem.zst")
    assert e.value.status_code == 403
