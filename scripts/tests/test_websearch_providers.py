import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tcode_agent.tools.base import ToolContext
from tcode_agent.tools.builtin.websearch import BRAVE_ENDPOINT, WebSearchTool


@pytest.mark.parametrize(
    "provider,key,status,payload,categories,expected,request_count",
    [
        ("", "test-secret", 200, {"results": [{"title": "UniProt", "url": "https://uniprot.org"}]}, "general", "UniProt", 1),
        ("brave", "test-secret", 200, {"web": {"results": [{"title": "UniProt", "url": "https://uniprot.org", "description": "Protein database"}]}}, "auto", "Protein database", 1),
        ("brave", "test-secret", 200, {}, "auto", "No results", 1),
        ("brave", "", 200, {}, "auto", "requires", 0),
        ("brave", "test-secret", 401, {}, "auto", "authentication", 1),
        ("brave", "test-secret", 429, {}, "auto", "quota", 1),
        ("brave", "test-secret", 200, TimeoutError(), "auto", "timed out", 1),
        ("brave", "test-secret", 302, {}, "auto", "HTTP 302", 1),
        ("brave", "test-secret", 200, {}, "images", "only supports", 0),
        ("invalid", "test-secret", 200, {}, "auto", "Select searxng or brave", 0),
    ],
)
def test_websearch_routes_without_fallback(monkeypatch, tmp_path, provider, key, status, payload,
                                           categories, expected, request_count):
    monkeypatch.setenv("TCODE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("TCODE_WEBSEARCH_PROVIDER", provider)
    monkeypatch.setenv("TCODE_BRAVE_SEARCH_API_KEY", key)
    monkeypatch.setenv("TCODE_SEARXNG_BASE_URL", "http://localhost:8888")
    response = MagicMock(status=status, ok=status == 200, json=AsyncMock(return_value=payload))
    if isinstance(payload, Exception):
        response.json.side_effect = payload
    session = MagicMock()
    session.__aenter__.return_value = session
    session.get.return_value.__aenter__.return_value = response
    monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
    result = asyncio.run(WebSearchTool().execute(
        {"query": "UniProt", "count": 1, "candidate_count": 1, "categories": categories},
        ToolContext(session_id="test", message_id="test", agent="test", abort=None),
    ))
    assert expected in (result.error or result.output)
    assert "test-secret" not in repr(result)
    assert session.get.call_count == request_count
    if request_count:
        endpoint = BRAVE_ENDPOINT if provider == "brave" else "http://localhost:8888/search"
        assert session.get.call_args.args == (endpoint,)
        kwargs = session.get.call_args.kwargs
        if provider == "brave":
            assert kwargs["headers"]["X-Subscription-Token"] == key
            assert kwargs["allow_redirects"] is False
        else:
            assert "X-Subscription-Token" not in kwargs["headers"]


def test_websearch_settings_are_write_only(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tcode_agent.server import websearch_config_api as api

    monkeypatch.setenv("TCODE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("TCODE_SEARXNG_BASE_URL", "http://localhost:8888")
    monkeypatch.delenv("TCODE_BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TCODE_WEBSEARCH_PROVIDER", raising=False)
    monkeypatch.setattr(api, "_authorize", lambda request: None)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    path = "/api/config/websearch"
    assert client.get(path).json()["provider"] == "searxng"
    assert client.put(path, json={"provider": "brave"}).status_code == 400
    assert client.put(path, json={"provider": "searxng", "searxng_url": "http://user:pass@localhost"}).status_code == 400
    saved = client.put(path, json={"provider": "brave", "brave_api_key": "private-test-key"})
    assert saved.status_code == 200
    assert saved.json()["brave_key_configured"] is True
    assert "private-test-key" not in saved.text
    assert "private-test-key" not in client.get(path).text
    assert client.put(path, json={"provider": "searxng"}).json()["brave_key_configured"]
    assert client.put(path, json={"provider": "brave"}).status_code == 200
    assert api.config.brave_key(api.config.read_settings()) == "private-test-key"


@pytest.mark.parametrize("provider,status,payload,ok,reason", [
    ("searxng", 200, {"results": []}, True, ""),
    ("searxng", 200, {}, False, "搜索结果列表"),
    ("searxng", 403, {}, False, "JSON"),
    ("searxng", 200, TimeoutError(), False, "超时"),
    ("brave", 200, {"web": {"results": [{"url": "https://example.org"}]}}, True, ""),
    ("brave", 401, {"secret": "private-test-key"}, False, "authentication"),
    ("brave", 429, {}, False, "quota"),
])
def test_connection_only_searches_on_explicit_post(monkeypatch, tmp_path, provider, status, payload, ok, reason):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tcode_agent.server import websearch_config_api as api

    monkeypatch.setenv("TCODE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("TCODE_SEARXNG_BASE_URL", "http://localhost:8888")
    monkeypatch.setattr(api, "_authorize", lambda request: None)
    response = MagicMock(status=status, ok=status == 200, json=AsyncMock(return_value=payload))
    if isinstance(payload, Exception):
        response.json.side_effect = payload
    session = MagicMock()
    session.__aenter__.return_value = session
    session.get.return_value.__aenter__.return_value = response
    monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    path = "/api/config/websearch"
    assert client.get(path).status_code == 200
    assert client.put(path, json={"provider": provider, "brave_api_key": "private-test-key"}).status_code == 200
    assert client.post(path + "/test").status_code == 415
    session.get.assert_not_called()
    result = client.post(path + "/test", json={})
    assert result.status_code == 200
    assert result.json()["ok"] is ok
    assert reason in result.json()["reason"]
    assert result.json()["tested_at"]
    assert "private-test-key" not in result.text
    assert session.get.call_count == 1
    assert session.get.call_args.args == (
        BRAVE_ENDPOINT if provider == "brave" else "http://localhost:8888/search",
    )
    assert session.get.call_args.kwargs["allow_redirects"] is False
