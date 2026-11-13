"""Exercise the Web model API and its real provider transport without external calls."""

import base64
import json
import os
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tcode_agent.config.manager import ConfigManager
from tcode_agent.providers.base import ChatMessage
from tcode_agent.providers.factory import clone_provider, create_provider
from tcode_agent.providers.web_model_transport import guarded_client, validate_base_url
from tcode_agent.server import web_model_api
from tcode_agent.vision import select_vision_model_ref
from tcode_agent.web_model_settings import settings_path

API = "/api/config/web-models"
SECRET = "fixture-key-not-a-real-credential"


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setenv("TCODE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("TCODE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("TCODE_ENTERPRISE_MODE", "0")
    monkeypatch.setenv("TCODE_WEB_MODEL_LOOPBACK", "1")
    monkeypatch.delenv("TCODE_DESKTOP_MANAGED", raising=False)
    monkeypatch.setattr(web_model_api.ServerState, "_workdir", str(tmp_path))
    app = FastAPI()
    app.include_router(web_model_api.router)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 41001)) as client:
        yield client, tmp_path
    ConfigManager.reload(str(tmp_path), emit=False)


@pytest.fixture
def gateway():
    calls = []
    colors = {(255, 0, 0): "red", (0, 0, 255): "blue", (0, 180, 0): "green", (255, 255, 0): "yellow"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls.append((self.path, self.headers.get("Authorization"), body))
            model = body["model"]
            if model == "redirect":
                self.send_response(307)
                self.send_header("Location", "/redirected")
                self.end_headers()
                return
            if model == "error":
                self.send_response(401)
                self.end_headers()
                return
            message = body["messages"][0]["content"]
            answer = "OK"
            if isinstance(message, list) and model == "vision":
                raw = base64.b64decode(message[1]["image_url"]["url"].split(",", 1)[1])
                first_idat = raw.index(b"IDAT")
                length = struct.unpack("!I", raw[first_idat - 4:first_idat])[0]
                scanline = zlib.decompress(raw[first_idat + 4:first_idat + 4 + length])
                answer = " ".join(colors[tuple(scanline[i:i + 3])] for i in range(1, 1 + 4 * 64 * 3, 64 * 3))
            if model == "empty":
                answer = ""
            output = json.dumps({"model": model, "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": answer}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", calls
        finally:
            server.shutdown()
            thread.join()


def add(client, base, **extra):
    return client.post(API + "/services", json={
        "name": "Fixture", "protocol": "openai_compatible", "base_url": base,
        "models": ["text", "vision", "error", "redirect", "empty"], "api_key": SECRET, **extra,
    })


def test_model_capabilities_defaults_and_private_storage(instance, gateway, monkeypatch):
    client, root = instance
    base, calls = gateway
    file_config = root / "tcode.json"
    file_config.write_text(json.dumps({"model": "file/legacy", "provider": {"file": {
        "npm": "@ai-sdk/openai-compatible", "options": {"baseURL": base, "apiKeyEnv": "FIXTURE_FILE_KEY"},
        "models": {"legacy": {}, "legacy-vision": {"supports_images": True}},
    }}}))
    monkeypatch.setenv("FIXTURE_FILE_KEY", "fixture-file-key")
    original = file_config.read_bytes()
    created = add(client, base)
    assert created.status_code == 200 and SECRET not in created.text
    service = created.json()["services"][0]
    prefix = "web-model-" + service["id"] + "/"
    assert service["key_saved"] and not service["results"]
    assert created.json()["default_chat"] == "file/legacy"
    assert not os.environ.get("TCODE_WEB_MODEL_" + service["id"].upper())

    def probe(model, purpose):
        response = client.post(API + "/test", json={"ref": prefix + model, "purpose": purpose})
        assert response.status_code == 200, response.text
        assert SECRET not in response.text
        return response.json()

    assert client.put(API + "/defaults", json={"purpose": "vision_model", "ref": prefix + "vision"}).status_code == 400
    assert probe("text", "text")["ok"]
    assert not probe("text", "image")["ok"]
    assert probe("vision", "image")["ok"]
    assert not probe("empty", "text")["ok"]
    assert "Key" in probe("error", "text")["reason"]
    assert not probe("redirect", "text")["ok"]
    assert all(url == "/v1/chat/completions" for url, _, _ in calls)
    assert [p["model"] for _, _, p in calls] == ["text", "text", "vision", "empty", "error", "redirect"]
    assert all(auth == f"Bearer {SECRET}" for _, auth, _ in calls)
    assert client.post(API + "/test", json={"ref": "file/legacy", "purpose": "text"}).json()["ok"]
    assert calls[-1][1] == "Bearer fixture-file-key"
    for purpose, model in (("model", "text"), ("vision_model", "vision")):
        assert client.put(API + "/defaults", json={"purpose": purpose, "ref": prefix + model}).status_code == 200
    cfg = ConfigManager.reload(str(root), emit=False)
    assert cfg["model"] == prefix + "text"
    assert select_vision_model_ref(cfg) == prefix + "vision"
    assert SECRET not in json.dumps(cfg)
    assert file_config.read_bytes() == original
    assert client.get(API).json()["services"][0]["results"]["vision"]["image"]["ok"]
    if os.name != "nt":
        assert settings_path().stat().st_mode & 0o077 == 0

    response = add(client, "https://provider.example/v1", id=service["id"], api_key=None)
    assert response.status_code == 400
    response = add(client, base, id=service["id"], api_key="fixture-rotated-key")
    assert response.status_code == 200
    assert response.json()["services"][0]["results"] == {}
    assert not response.json()["overrides"]["vision_model"]
    assert client.put(API + "/defaults", json={"purpose": "model", "ref": ""}).json()["default_chat"] == "file/legacy"
    assert file_config.read_bytes() == original


def test_web_admin_boundary(instance, monkeypatch):
    client, _ = instance
    assert client.get(API).status_code == 200
    assert client.get(API, headers={"Host": "example.com"}).status_code == 403
    assert client.get(API, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert client.post(API + "/services", json={}, headers={"Origin": "http://other.example"}).status_code == 403
    monkeypatch.setenv("TCODE_WEB_MODEL_LOOPBACK", "0")
    assert client.get(API).status_code == 403
    monkeypatch.setenv("TCODE_ENTERPRISE_MODE", "1")
    for role, tenant in (("member", web_model_api.workdirs.default_tenant_id()), ("owner", "other")):
        monkeypatch.setattr(web_model_api, "require_current_user", lambda _: SimpleNamespace(role=role, tenant_id=tenant))
        assert client.get(API).status_code == 403
    monkeypatch.setattr(web_model_api, "require_current_user", lambda _: SimpleNamespace(
        role="owner", tenant_id=web_model_api.workdirs.default_tenant_id()))
    assert client.get(API).status_code == 403
    assert client.get("https://example.test" + API).status_code == 200
    assert client.post("https://example.test" + API + "/services", json={
        "name": "SSRF", "models": ["m"], "protocol": "openai_compatible", "base_url": "http://127.0.0.1",
    }).status_code == 400
    monkeypatch.setenv("TCODE_DESKTOP_MANAGED", "1")
    assert client.get(API).status_code == 404


def test_legacy_web_picker_does_not_copy_overlay_to_file(instance, gateway):
    from tcode_agent.server.chat_setup import _persist_model_selection_for_workdir

    client, root = instance
    base, _ = gateway
    file_config = root / "tcode.json"
    file_config.write_text('{"model":"file/legacy","provider":{"file":{"models":{"legacy":{}}}}}')
    original = file_config.read_bytes()
    sid = add(client, base).json()["services"][0]["id"]
    ref = f"web-model-{sid}/text"
    assert _persist_model_selection_for_workdir(ref, workdir=str(root), prefer_local=False) == ref
    assert file_config.read_bytes() == original
    assert client.get(API).json()["default_chat"] == ref
    assert _persist_model_selection_for_workdir("file/legacy", workdir=str(root), prefer_local=False) == "file/legacy"
    assert file_config.read_bytes() == original
    assert client.get(API).json()["default_chat"] == "file/legacy"


def test_corrupt_optional_store_does_not_break_file_models(instance):
    client, root = instance
    (root / "tcode.json").write_text('{"model":"file/legacy","provider":{"file":{"models":{"legacy":{}}}}}')
    path = settings_path()
    path.parent.mkdir()
    path.write_text('{"services":{"bad":null}}')
    assert ConfigManager.reload(str(root), emit=False)["model"] == "file/legacy"
    assert client.get(API).status_code == 409


@pytest.mark.parametrize("url", [
    "http://example.org", "https://169.254.169.254", "https://[::ffff:127.0.0.1]",
    "https://224.0.0.1", "https://user@example.org", "https://localhost:1",
    "https://example.org/v1?token=test", "https://127.1", "https://metadata.google.internal",
])
def test_outbound_url_rejects_unsafe_destinations(url):
    with pytest.raises(ValueError):
        validate_base_url(url, allow_loopback=False)


@pytest.mark.anyio
async def test_dns_private_answer_is_rejected_before_connect(monkeypatch):
    import asyncio
    import socket
    async def private(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", private)
    async with guarded_client(allow_loopback=False) as client:
        with pytest.raises(httpx.ConnectError, match="未允许"):
            await client.get("https://public.example")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_new_runtime_and_clone_do_not_send_process_default_key(instance, gateway, monkeypatch):
    import asyncio
    client, root = instance
    base, calls = gateway
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-for-this-endpoint")
    payload = add(client, base, api_key="").json()
    ref = "web-model-" + payload["services"][0]["id"] + "/text"

    async def exercise():
        provider = create_provider(str(root), model_ref=ref)
        cloned = clone_provider(provider)
        try:
            for entry in (provider, cloned):
                answer = await entry.chat([ChatMessage(role="user", content="OK")])
                assert answer.content == "OK"
        finally:
            await provider.close()
            await cloned.close()

    asyncio.run(exercise())
    assert [key for _, key, _ in calls] == ["Bearer not-required", "Bearer not-required"]
