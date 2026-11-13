"""Offline guards for persistent plugin approval and original-job-only reconnect."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tcode_agent.mcp.manager_auto import MCPManager, MCPSessionExpiredError
from tcode_agent.runtime import remote_cancel, remote_recovery
from tcode_agent.runtime.shared_tracker import SharedTracker


@pytest.mark.parametrize("tool", ["get_job_status", "collect_job_result", "terminate_job"])
def test_confirmed_session_expiry_retries_only_the_original_job(monkeypatch, tool):
    ref = {"job_id": "original", "process_id": "owned", "job_token": "synthetic"}
    reply = {"job_id": "original"}
    call = AsyncMock(side_effect=[MCPSessionExpiredError("fixture"), reply])
    monkeypatch.setattr(MCPManager, "call_tool", call)
    assert asyncio.run(remote_cancel.call_job_tool(ref, tool)) == reply
    assert call.await_count == 2
    assert call.await_args_list[0] == call.await_args_list[1]
    assert call.await_args.args[1:] == (tool, {"input": ref})


def test_ambiguous_failure_and_submit_are_not_replayed(monkeypatch):
    call = AsyncMock(side_effect=TimeoutError("ambiguous"))
    monkeypatch.setattr(MCPManager, "call_tool", call)
    with pytest.raises(TimeoutError):
        asyncio.run(remote_cancel.call_job_tool({"job_id": "original"}, "terminate_job"))
    assert call.await_count == 1
    with pytest.raises(ValueError, match="unsupported"):
        asyncio.run(remote_cancel.call_job_tool({"job_id": "original"}, "submit_job"))
    assert call.await_count == 1


def test_pending_cancel_backoff_stays_bounded_after_long_outage(monkeypatch, tmp_path):
    wd = str(tmp_path)
    SharedTracker.upsert_issue({"id": "task", "identifier": "LOCAL-fixture", "state": "In Progress",
        "workspace": {"job_ref": {"job_id": "original"}}}, workdir=wd)
    SharedTracker.request_cancellation(task_id="task", workdir=wd)
    SharedTracker.patch_task(task_id="task", workspace_patch={"recovery": {"attempt": 8}}, workdir=wd)
    monkeypatch.setattr(remote_recovery, "cancel_remote_job", AsyncMock(side_effect=ConnectionError("offline")))
    asyncio.run(remote_recovery.reconcile_task("task", wd))
    row = SharedTracker.get_task(task_id="task", workdir=wd)
    assert row["status"] == "running"
    assert row["workspace"]["cancellation"]["state"] == "pending"
    assert 0 < row["workspace"]["recovery"]["next_retry_at"] - time.time() <= 10


@pytest.mark.parametrize("shutdown", [True, False])
def test_shutdown_releases_plugin_without_deactivating_target(monkeypatch, tmp_path, shutdown):
    from tcode_agent.plugins import async_targets
    from tcode_agent.plugins.manager import PluginManager
    from tcode_agent.plugins.registry import PluginRegistry
    from tcode_agent.runtime.workspace_registry import registry_workspace

    stop = AsyncMock()
    deactivate = AsyncMock()
    monkeypatch.setattr(async_targets, "deactivate_plugin", deactivate)
    monkeypatch.setattr(PluginManager, "_cleanup_plugin_registry_fallbacks", lambda *args, **kwargs: {})
    monkeypatch.setattr(MCPManager, "unregister_servers_by_source", AsyncMock())
    plugin = SimpleNamespace(name="fixture", source="fixture", instance_id="fixture",
        manifest={"asyncTargets": ["fixture.json"]}, instance_status=None, effect_scope=None,
        shutdown_handler=stop, post_shutdown_effect_scope=None)
    with registry_workspace(str(tmp_path)):
        PluginRegistry.register(plugin)
        asyncio.run(PluginManager(str(tmp_path)).unload_all(shutdown=shutdown))
        assert PluginRegistry.get("fixture") is None
    stop.assert_awaited_once()
    assert deactivate.await_count == (0 if shutdown else 1)


def test_startup_restores_only_inactive_approved_unchanged_targets(monkeypatch, tmp_path):
    from tcode_agent.plugins import async_targets as targets

    target = {"id": "compute", "connection": {}, "execution": {}, "tools": []}
    loaded = SimpleNamespace(name="fixture")
    monkeypatch.setattr(targets, "plugin_targets", lambda _: [target])
    monkeypatch.setattr("tcode_agent.server.desktop_config.remote_services_paused", lambda: False)
    gateway = AsyncMock(return_value={"version": "restored"})
    monkeypatch.setattr(targets, "gateway", gateway)
    wd = str(tmp_path)
    for owner, approved, active, changed in (
        ("legacy", True, False, False), ("revoked", False, False, False),
        ("changed", True, False, True), ("active", True, True, False),
    ):
        targets.save_binding(wd, owner, loaded.name, "compute", {
            "approved": approved, "active": active,
            "declaration_hash": "stale" if changed else targets.declaration_hash(target),
            "config": {"compute_url": "http://127.0.0.1:1/mcp"},
        })
    asyncio.run(targets.restore_plugin(wd, loaded, inactive_only=True))
    gateway.assert_awaited_once()
    assert gateway.await_args.args[2]["owner"] == "legacy"
    assert targets.binding(wd, "legacy", loaded.name, "compute")["active"]
    assert not targets.binding(wd, "revoked", loaded.name, "compute")["active"]
    assert not targets.binding(wd, "changed", loaded.name, "compute")["active"]
