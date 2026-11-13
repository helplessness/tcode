import json
import os
import sys
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


@pytest.mark.parametrize("config,expected", [
    (None, False),
    ({}, False),
    ({"job-protocol-mcp": {"url": "https://jobs.example.org/mcp"}}, False),
    ({"job-protocol-mcp": {"url": "http://localhost:10121/mcp"}}, True),
    ({"$desktop": {"managed_job_protocol": True}}, True),
])
def test_desktop_only_starts_configured_managed_gateway(monkeypatch, tmp_path, config, expected):
    path = tmp_path / "desktop-mcp.json"
    monkeypatch.setenv("TCODE_DESKTOP_MCP_CONFIG", str(path))
    monkeypatch.delenv("TCODE_DESKTOP_PAUSE_REMOTE_SERVICES", raising=False)
    monkeypatch.delenv("TCODE_DESKTOP_MANAGED_JOB_PROTOCOL", raising=False)
    if config is not None:
        path.write_text(json.dumps(config))
    start = Mock(return_value=(object(), "http://localhost:10121/mcp"))
    monkeypatch.setattr(desktop_launcher, "start_managed_job_protocol", start)
    result = desktop_launcher.start_optional_job_protocol(tmp_path, tmp_path)
    assert bool(result[1]) is expected
    assert start.call_count == int(expected)


def test_missing_artifact_has_configuration_hint(monkeypatch):
    from tcode_agent.artifact_services import artifact_service

    for key in ("TMCP_ARTIFACT_SERVICES", "TMCP_ARTIFACT_TARGET_SERVICES", "TMCP_ARTIFACT_SERVICE_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="file transfer is not configured"):
        artifact_service()
