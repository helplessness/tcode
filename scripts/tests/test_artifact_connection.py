from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tcode_agent.server import artifact_services_api as api


@pytest.mark.parametrize("mode,status,expected", [
    ("restricted", 200, "connected"),
    ("local_trusted", 200, "reachable"),
    ("restricted", 401, "failed"),
])
def test_artifact_health_never_exposes_credentials(monkeypatch, mode, status, expected):
    url = "https://artifact.example.org"
    monkeypatch.delenv("TCODE_DESKTOP_MANAGED", raising=False)
    monkeypatch.setenv("TMCP_ARTIFACT_SERVICES", '{"store":{"url":"https://artifact.example.org","token_env":"TEST_ARTIFACT_TOKEN"}}')
    monkeypatch.setenv("TMCP_ARTIFACT_TARGET_SERVICES", "{}")
    monkeypatch.setenv("TMCP_ARTIFACT_SERVICE_ID", "store")
    monkeypatch.setenv("TEST_ARTIFACT_TOKEN", "secret-value")
    monkeypatch.setattr(api, "require_current_user", lambda request: SimpleNamespace(
        role="admin", tenant_id=api.workdirs.default_tenant_id()))
    remote = MagicMock()
    remote.__aenter__.return_value = remote
    remote.get = AsyncMock(return_value=httpx.Response(status, json={"status": "ok", "mode": mode}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: remote)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    path = "/api/artifact-services/test"
    payload = {"service_id": "store", "url": url, "token_env": "TEST_ARTIFACT_TOKEN"}
    assert client.post(path, json={**payload, "url": "https://other.example.org"}).json()["state"] == "unverified"
    remote.get.assert_not_called()
    response = client.post(path, json=payload)
    assert response.json()["state"] == expected
    assert "secret-value" not in response.text
    remote.get.assert_awaited_once_with(url + "/health", headers={"X-Artifact-Token": "secret-value"})
