import json
from contextlib import ExitStack, nullcontext
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tcode_agent.server import desktop_launcher, launcher


def test_local_profile_is_explicit_and_loopback_only(monkeypatch, tmp_path):
    import importlib

    app = importlib.import_module("tcode_agent.server.app")
    monkeypatch.setattr(app, "run_server", Mock())
    monkeypatch.setattr(app, "create_static_dir", Mock())
    load = Mock()
    monkeypatch.setattr(launcher, "_load_env_file", load)
    for key in ("TCODE_WORKDIR", "TCODE_CONFIG_ROOT", "TCODE_ENV_FILE", "TCODE_AUTH_REQUIRED",
                "TCODE_ENTERPRISE_MODE", "TCODE_SANDBOX_ENABLED", "TCODE_SANDBOX_REQUIRE_RUNNER",
                "TCODE_ASYNC_WATCHER_ENABLED", "TCODE_RESEARCH_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TCODE_ENV_FILE", "deployment.env")
    monkeypatch.setattr(sys, "argv", ["tcode-server", "--workdir", str(tmp_path)])
    launcher.main()
    load.assert_called_once_with("deployment.env")
    load.reset_mock()
    monkeypatch.setattr(sys, "argv", ["tcode-server", "--local", "--workdir", str(tmp_path)])
    launcher.main()
    load.assert_not_called()
    assert os.environ["TCODE_ENV_FILE"] == os.devnull
    assert os.environ["TCODE_ASYNC_WATCHER_ENABLED"] == "0"
    assert os.environ["TCODE_SANDBOX_ENABLED"] == "false"
    monkeypatch.setattr(sys, "argv", ["tcode-server", "--local", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        launcher.main()


def test_web_reuses_legacy_bridge_gateway(monkeypatch, tmp_path):
    from tcode_agent.runtime.bridge_worker import _resolve_bridge_mcp_config
    from tcode_agent.server import managed_job_protocol as managed

    primary = tmp_path / "mcp.json"
    primary.write_text(json.dumps({"echo-mcp": {"invocation_mode": "async"}}))
    legacy = tmp_path / "config/mcp_job_protocol_only.json"
    legacy.parent.mkdir()
    broker = {"url": "http://127.0.0.1:10121/mcp", "headers": {"X-Synthetic": "test"}}
    legacy.write_text(json.dumps({"job-protocol-mcp": broker}))
    monkeypatch.setenv("TCODE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("TCODE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("TCODE_MCP_CONFIG", str(primary))
    monkeypatch.setenv("TCODE_BRIDGE_MCP_CONFIG", "")
    monkeypatch.delenv("TCODE_INTERNAL_EXTERNAL_JOB_PROTOCOL", raising=False)
    monkeypatch.setenv("TCODE_MANAGED_JOB_PROTOCOL", "1")
    start = Mock(side_effect=AssertionError("external broker must win"))
    monkeypatch.setattr(managed, "managed_job_protocol", start)
    args = SimpleNamespace(local=False, host="127.0.0.1", port=8080, reload=False)
    with ExitStack() as stack:
        launcher._configure_job_protocol(stack, args, str(tmp_path))
        assert managed.effective_config({})["job-protocol-mcp"] == broker
        assert _resolve_bridge_mcp_config() == str(legacy)
    start.assert_not_called()


@pytest.mark.parametrize("config,expected", [
    (None, False),
    ({}, False),
    ({"job-protocol-mcp": {"url": "https://jobs.example.org/mcp"}}, False),
    ({"job-protocol-mcp": {"url": "http://localhost:10121/mcp"}}, False),
    ({"$desktop": {"managed_job_protocol": True}}, True),
    ({"echo-mcp": {"invocation_mode": "async", "url": "http://127.0.0.1:12345/mcp"}}, True),
    ({"$desktop": {"managed_job_protocol": True}, "job-protocol-mcp": {"url": "https://jobs.example.org/mcp"}}, False),
])
def test_desktop_only_starts_configured_managed_gateway(monkeypatch, tmp_path, config, expected):
    path = tmp_path / "desktop-mcp.json"
    monkeypatch.setenv("TCODE_DESKTOP_MCP_CONFIG", str(path))
    monkeypatch.delenv("TCODE_DESKTOP_PAUSE_REMOTE_SERVICES", raising=False)
    monkeypatch.delenv("TCODE_DESKTOP_MANAGED_JOB_PROTOCOL", raising=False)
    monkeypatch.delenv("TCODE_MANAGED_JOB_PROTOCOL", raising=False)
    monkeypatch.delenv("TCODE_PLUGIN_JOB_PROTOCOL_URL", raising=False)
    if config is not None:
        path.write_text(json.dumps(config))
    from tcode_agent.server import managed_job_protocol
    start = Mock(return_value=nullcontext("http://127.0.0.1:10121/mcp"))
    monkeypatch.setattr(managed_job_protocol, "managed_job_protocol", start)
    with ExitStack() as stack:
        result = desktop_launcher.start_optional_job_protocol(tmp_path, tmp_path, stack)
    assert bool(result[1]) is expected
    assert start.call_count == int(expected)


def test_legacy_desktop_store_is_not_replaced(monkeypatch, tmp_path):
    from tcode_agent.server import managed_job_protocol

    path = tmp_path / "desktop-mcp.json"
    path.write_text(json.dumps({"job-protocol-mcp": {"url": "http://localhost:10121/mcp"}}))
    state = tmp_path / ".tcode/job-protocol"
    state.mkdir(parents=True)
    (state / "jobs.sqlite3").touch()
    monkeypatch.setenv("TCODE_DESKTOP_MCP_CONFIG", str(path))
    monkeypatch.delenv("TCODE_PLUGIN_JOB_PROTOCOL_URL", raising=False)
    legacy = Mock(return_value=(object(), "http://127.0.0.1:10121/mcp"))
    builtin = Mock(side_effect=AssertionError("must not replace legacy jobs"))
    monkeypatch.setattr(desktop_launcher, "start_managed_job_protocol", legacy)
    monkeypatch.setattr(managed_job_protocol, "managed_job_protocol", builtin)
    with ExitStack() as stack:
        assert desktop_launcher.start_optional_job_protocol(tmp_path, tmp_path, stack)[1].endswith("/mcp")
    legacy.assert_called_once()
    builtin.assert_not_called()


def test_missing_artifact_has_configuration_hint(monkeypatch):
    from tcode_agent.artifact_services import artifact_service

    for key in ("TMCP_ARTIFACT_SERVICES", "TMCP_ARTIFACT_TARGET_SERVICES", "TMCP_ARTIFACT_SERVICE_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="file transfer is not configured"):
        artifact_service()
