"""Exercise the real Streamable HTTP broker and durable echo recovery."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import socket
import sys

import httpx
import pytest
import uvicorn

from tcode_agent.server.managed_job_protocol import (
    LOCAL_TOKEN, WORKER_HEADER, WORKER_TOKEN, bridge_worker_environment, managed_job_protocol,
)


@asynccontextmanager
async def echo_server():
    from fastmcp import FastMCP

    server = FastMCP("Disposable echo")
    calls = {}
    remote = {}

    @server.tool
    def echo_text(text: str) -> dict:
        calls[text] = calls.get(text, 0) + 1
        return {"text": text}

    @server.tool
    def submit_echo(request_id: str, tool: str, arguments: dict) -> dict:
        calls[request_id] = calls.get(request_id, 0) + 1
        remote.setdefault(request_id, {"status": "running", "text": arguments["text"]})
        if arguments["text"] == "lost echo receipt":
            raise RuntimeError("echo ran, but no job receipt was returned")
        return {"job_id": request_id}

    @server.tool
    def echo_status(job_id: str) -> dict:
        remote[job_id]["polled"] = True
        return {"status": remote[job_id]["status"]}

    @server.tool
    def echo_result(job_id: str) -> dict:
        return {"text": remote[job_id]["text"]}

    @server.tool
    def cancel_echo(job_id: str) -> dict:
        remote[job_id]["status"] = "cancelled"
        return {"status": "cancelled"}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        http = uvicorn.Server(uvicorn.Config(server.http_app(), log_level="error"))
        task = asyncio.create_task(http.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not http.started:
                    await asyncio.sleep(0.02)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", calls, remote
        finally:
            http.should_exit = True
            with suppress(asyncio.CancelledError):
                await task


def test_real_echo_and_remote_job_restart(tmp_path, monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from tcode_agent.runtime.remote_cancel import cancellation_receipt

    monkeypatch.delenv("TMCP_TARGET_REGISTRY_TOKEN", raising=False)
    config = tmp_path / "mcp.json"
    scope = {"tenant_id": "test", "user_id": "alice", "workspace_id": "work"}
    name = "plugin-" + "a" * 32

    async def call(client, tool, data):
        result = await client.call_tool(tool, {"input": data})
        return result.structured_content

    def client(url, *, worker=True):
        headers = {"Authorization": "Bearer " + os.environ[LOCAL_TOKEN]}
        if worker:
            headers[WORKER_HEADER] = bridge_worker_environment()[WORKER_TOKEN]
        return Client(StreamableHttpTransport(url, headers=headers))

    async def check():
        async with echo_server() as (endpoint, calls, remote), echo_server() as (new_endpoint, new_calls, _):
            config.write_text(json.dumps({"simple-demo-mcp": {"url": endpoint, "invocation_mode": "async"},
                                          "boltz-mcp": {"url": endpoint, "invocation_mode": "async"}}))
            data = {**scope, "run_id": "run-echo", "target_server": "simple-demo-mcp",
                    "target_tool": "echo_text", "arguments": {"text": "local echo"}}
            with managed_job_protocol(tmp_path, configs=[config]) as url:
                with pytest.raises(BlockingIOError):
                    with managed_job_protocol(tmp_path, configs=[config]):
                        pytest.fail("started a second gateway for the same state")
                async with httpx.AsyncClient() as http:
                    base = url.removesuffix("/mcp")
                    registry = base + "/api/v1/target-registrations"
                    assert (await http.get(registry)).status_code == 401
                    auth = {"X-Target-Registry-Token": os.environ["TMCP_TARGET_REGISTRY_TOKEN"]}
                    assert (await http.get(registry, headers=auth)).json()["configured_servers"] == ["simple-demo-mcp"]
                    target = {"server": name, "owner": "test/alice", "plugin": "demo", "url": endpoint,
                              "execution": dict(mode="remote-job", submitTool="submit_echo", statusTool="echo_status",
                                                resultTool="echo_result", cancelTool="cancel_echo"),
                              "tools": [{"name": "echo_text", "inputSchema": {"type": "object", "required": ["text"],
                                         "properties": {"text": {"type": "string"}}}, "output": {"contract": "json"}}]}
                    registered = await http.put(registry + "/" + name, headers=auth, json=target)
                    assert registered.status_code == 200
                    version = registered.json()["version"]
                    assert (await http.get(url)).status_code == 401
                async with client(url, worker=False) as mcp:
                    assert (await mcp.call_tool("submit_job", {"input": data}, raise_on_error=False)).is_error
                    assert "target_version" in await call(mcp, "describe_target_tool", data)
                async with client(url) as mcp:
                    bad = await call(mcp, "describe_target_tool", {**data, "arguments": {}})
                    assert bad["validation"]["errors"][0]["field"] == "text"
                    data["target_version"] = bad["target_version"]
                    assert (await call(mcp, "submit_job", {**data, "command": ""}))["submission_state"] == "not_sent"
                    assert not calls
                    receipt = await call(mcp, "submit_job", data)
                    assert (await call(mcp, "submit_job", data))["job_id"] == receipt["job_id"]
                    invalid = await call(mcp, "submit_job", {**data, "arguments": {"text": "changed"}})
                    assert invalid["submission_state"] == "not_sent"
                    async with asyncio.timeout(5):
                        while (await call(mcp, "get_job_status", receipt))["status"] != "completed":
                            await asyncio.sleep(0.05)
                    assert (await call(mcp, "collect_job_result", receipt))["target_result"]["text"] == "local echo"
                    base_data = {**data, "target_server": name, "target_version": version}
                    remote_receipt = await call(mcp, "submit_job", {**base_data, "run_id": "remote-echo"})
                    cancel_receipt = await call(mcp, "submit_job", {**base_data, "run_id": "cancel-echo"})
                    lost = await call(mcp, "submit_job", {**base_data, "run_id": "lost-echo",
                                                         "arguments": {"text": "lost echo receipt"}})
                    async with asyncio.timeout(5):
                        while not all(calls.get(r["job_id"]) for r in (remote_receipt, cancel_receipt)):
                            await asyncio.sleep(0.05)
                        # Wait for the broker to persist the original remote ID.
                        while not all(remote[r["job_id"]].get("polled") for r in (remote_receipt, cancel_receipt)):
                            for item in (remote_receipt, cancel_receipt):
                                await call(mcp, "get_job_status", item)
                            await asyncio.sleep(0.05)
                        while (await call(mcp, "get_job_status", lost)).get("submission_state") != "unknown":
                            await asyncio.sleep(0.05)
                async with httpx.AsyncClient() as http:
                    changed = await http.put(registry + "/" + name, headers=auth, json={**target, "url": new_endpoint})
                    assert changed.is_success and changed.json()["version"] != version
            with managed_job_protocol(tmp_path, configs=[config]) as url:
                async with client(url) as mcp:
                    assert (await call(mcp, "collect_job_result", receipt))["target_result"]["text"] == "local echo"
                    assert (await call(mcp, "get_job_status", remote_receipt))["status"] == "running"
                    remote[remote_receipt["job_id"]]["status"] = "completed"
                    assert (await call(mcp, "collect_job_result", remote_receipt))["target_result"]["text"] == "local echo"
                    cancelled = await call(mcp, "terminate_job", cancel_receipt)
                    assert cancellation_receipt(cancelled, cancel_receipt)["cancelled"]
                    stale = await call(mcp, "submit_job", {**base_data, "run_id": "queued-before-change"})
                    assert stale["submission_state"] == "not_sent"
                    unpinned = {key: value for key, value in base_data.items() if key != "target_version"}
                    assert (await call(mcp, "submit_job", {**unpinned, "run_id": "old-unpinned"}))["submission_state"] == "not_sent"
                    assert (await call(mcp, "get_job_status", lost))["submission_state"] == "unknown"
                    wrong = await mcp.call_tool("get_job_status", {"input": {**remote_receipt, "job_token": "wrong"}}, raise_on_error=False)
                    assert wrong.is_error
            assert calls == {"local echo": 1, remote_receipt["job_id"]: 1, cancel_receipt["job_id"]: 1, lost["job_id"]: 1}
            assert not new_calls

    asyncio.run(check())


def test_local_submit_keeps_third_party_arguments(tmp_path, monkeypatch):
    from tcode_agent.mcp._symphony import build_job_protocol_submit_input
    from tcode_agent.tools.base import ToolContext

    monkeypatch.setenv("TCODE_INTERNAL_JOB_PROTOCOL_URL", "http://127.0.0.1:10121/mcp")
    monkeypatch.setenv("TCODE_ASYNC_EXECUTION_MODE", "bridge")
    context = ToolContext(session_id="test", message_id="message", agent="test", abort=None,
                          extra={"workdir": str(tmp_path)})
    data = build_job_protocol_submit_input(server_name="echo-mcp", tool_name="echo_text",
        tool_args={"text": "plain echo"}, context=context, issue_id="echo", issue_identifier="LOCAL-ECHO",
        description="test", input_schema=None)
    assert data["arguments"] == {"text": "plain echo"}


def test_agent_proxy_does_not_forward_empty_command(monkeypatch):
    from tcode_agent.mcp._proxy import _simplified_submit_args

    monkeypatch.setenv("TCODE_INTERNAL_JOB_PROTOCOL_URL", "http://127.0.0.1:10121/mcp")
    parsed, error = _simplified_submit_args({"input": {"command": "", "target_tool": "echo_text"}})
    assert parsed is None and "command" in error.error


def test_enqueue_pins_approved_plugin_version_without_dynamic_headers(tmp_path, monkeypatch):
    from tcode_agent.mcp._symphony import enqueue_symphony_local_issue
    from tcode_agent.plugins.async_targets import save_binding
    from tcode_agent.runtime.shared_tracker import SharedTracker
    from tcode_agent.tools.base import ToolContext

    workdir = str(tmp_path)
    server = "plugin-" + "a" * 32
    monkeypatch.setenv("TCODE_INTERNAL_JOB_PROTOCOL_URL", "http://127.0.0.1:10121/mcp")
    monkeypatch.setenv("TCODE_ASYNC_EXECUTION_MODE", "bridge")
    monkeypatch.setenv("TCODE_WORKDIR", workdir)
    monkeypatch.setenv("TCODE_CONFIG_ROOT", workdir)
    save_binding(workdir, "test/alice", "mcp-config", "echo",
                 {"active": True, "origin": "mcp-config", "server": server, "version": "approved-v1",
                  "declaration": {"tools": []}})
    context = ToolContext(session_id="echo-session", message_id="message", agent="test", abort=None,
                          extra={"workdir": workdir, "tenant_id": "test", "user_id": "alice", "workspace_id": "echo"})
    accepted = asyncio.run(enqueue_symphony_local_issue(
        server_name=server, tool_name="echo_text", normalized_args={"text": "queued echo"}, context=context,
    ))
    row = SharedTracker.get_task(identifier=accepted["identifier"], workdir=workdir)
    assert row["submit_input"]["target_version"] == "approved-v1"
    assert row["submit_input"]["arguments"] == {"text": "queued echo"}
    assert row["submit_input"]["workspace"]["job_protocol_backend"] == "tcode-local"


def test_bridge_reports_missing_echo_receipt_without_reposting(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tcode_agent.runtime import bridge_worker as bridge

    monkeypatch.setenv("TCODE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("TCODE_INTERNAL_JOB_PROTOCOL_URL", "http://127.0.0.1:10121/mcp")
    monkeypatch.setattr(bridge, "_local_worker_token", "test-only")
    monkeypatch.setattr(bridge, "_init_mcp", AsyncMock())
    monkeypatch.setattr(bridge.MCPManager, "get_status", AsyncMock(return_value={
        "job-protocol-mcp": SimpleNamespace(status="connected"),
    }))
    ref = {"job_id": "tcode-job-echo", "process_id": "tcode-job-echo"}
    call = AsyncMock(side_effect=[ref, {**ref, "status": "running", "submission_state": "unknown"}])
    monkeypatch.setattr(bridge.MCPManager, "call_tool", call)
    for name in ("_attach_job_ref", "_heartbeat", "_get_task", "_fail", "_complete"):
        monkeypatch.setattr(bridge, name, AsyncMock(return_value={}))
    asyncio.run(bridge._run_task(None, {"task_id": "echo", "claim_token": "test", "submit_input": {
        "target_server": "plugin-" + "a" * 32, "target_tool": "echo_text", "arguments": {"text": "echo"},
    }}))
    assert [entry.args[1] for entry in call.await_args_list] == ["submit_job", "get_job_status"]
    assert bridge._heartbeat.await_args.kwargs["workspace_patch"]["recovery"]["state"] == "submission_unknown"
    bridge._fail.assert_not_awaited()
    bridge._complete.assert_not_awaited()


def test_recovery_never_sends_reference_to_different_broker(monkeypatch):
    from unittest.mock import AsyncMock
    from tcode_agent.mcp.manager_auto import MCPManager
    from tcode_agent.runtime.remote_cancel import call_job_tool

    call = AsyncMock()
    monkeypatch.setattr(MCPManager, "call_tool", call)
    monkeypatch.setenv("TCODE_INTERNAL_JOB_PROTOCOL_URL", "http://127.0.0.1:10121/mcp")
    with pytest.raises(ValueError, match="原任务"):
        asyncio.run(call_job_tool({"job_id": "old-tmcp-job"}, "get_job_status"))
    monkeypatch.delenv("TCODE_INTERNAL_JOB_PROTOCOL_URL")
    with pytest.raises(ValueError, match="原任务"):
        asyncio.run(call_job_tool({"job_id": "tcode-job-demo"}, "terminate_job"))
    call.assert_not_awaited()


def test_tracker_token_without_posix_fchmod(tmp_path, monkeypatch):
    from tcode_agent.runtime.tracker_token import ensure_tracker_token

    monkeypatch.delattr(os, "fchmod", raising=False)
    env = {"TCODE_TRACKER_API_TOKEN_FILE": str(tmp_path / "tracker.token")}
    token = ensure_tracker_token(env)
    assert (tmp_path / "tracker.token").read_text().strip() == token


def test_single_web_launcher_runs_echo_through_tracker(tmp_path):
    pytest.importorskip("fastmcp")

    async def check():
        async with echo_server() as (endpoint, calls, _):
            config = tmp_path / "mcp.json"
            config.write_text(json.dumps({"simple-demo-mcp": {"url": endpoint, "invocation_mode": "async"}}))
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            root = Path(__file__).resolve().parents[2]
            env = {key: value for key, value in os.environ.items() if not key.startswith(("TCODE_", "TMCP_", "SYMPHONY_"))}
            env.update(TCODE_CONFIG_ROOT=str(tmp_path), TCODE_WORKDIR=str(tmp_path),
                       TCODE_MCP_CONFIG=str(config), TCODE_ENV_FILE=os.devnull, TCODE_AUTH_REQUIRED="0",
                       TCODE_ENTERPRISE_MODE="0", TCODE_SANDBOX_ENABLED="false",
                       TCODE_RESEARCH_ENABLED="false", TCODE_TRACKER_API_TOKEN="disposable-test-token",
                       TCODE_ASYNC_EXECUTION_MODE="bridge", TCODE_BRIDGE_POLL_SECONDS="1",
                       TCODE_CLI_SHOW_TOOL_INIT_LOGS="0")
            log = tmp_path / "web.log"
            with log.open("wb") as output:
                web = await asyncio.create_subprocess_exec(sys.executable, "-m", "tcode_agent.server.launcher",
                    "--port", str(port), "--workdir", str(tmp_path), cwd=root, env=env, stdout=output, stderr=output)
                try:
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                                 headers={"X-Tracker-Token": "disposable-test-token"}) as http:
                        async with asyncio.timeout(20):
                            while True:
                                try:
                                    if (await http.get("/api/health")).is_success:
                                        break
                                except httpx.ConnectError:
                                    pass
                                if web.returncode is not None:
                                    raise AssertionError("Web stopped before startup")
                                await asyncio.sleep(0.1)
                        assert (tmp_path / ".tcode/job-protocol/web-bridge.pid").read_text() == str(web.pid)
                        payload = {"run_id": "echo-via-web", "target_server": "simple-demo-mcp", "target_tool": "echo_text",
                                   "arguments": {"text": "echo through Web Bridge"},
                                   "workspace": {"job_protocol_backend": "tcode-local"}}
                        from tcode_agent.server.job_protocol import _digest
                        payload["target_version"] = _digest(json.loads(config.read_text())["simple-demo-mcp"])[:24]
                        assert (await http.post("/api/tracker/tasks", headers={"X-Tracker-Token": ""},
                                                json={"task": {"id": "unauthorized"}})).status_code == 401
                        assert (await http.post("/api/tracker/claim-next", headers={"X-Tracker-Token": ""},
                                                json={"worker_id": "unauthorized", "channel": "bridge"})).status_code == 401
                        old = await http.post("/api/tracker/tasks", json={"task": {
                            "id": "old-echo", "identifier": "LOCAL-OLD", "kind": "symphony_local_issue",
                            "state": "Todo", "execution_channel": "bridge",
                            "description": "submit_job.input: " + json.dumps({**payload, "workspace": {}})}})
                        assert old.is_success
                        created = await http.post("/api/tracker/tasks", json={"task": {
                            "id": "echo-via-web", "identifier": "LOCAL-ECHOTEST", "title": "Disposable echo",
                            "kind": "symphony_local_issue", "state": "Todo", "execution_channel": "bridge",
                            "description": "submit_job.input: " + json.dumps(payload)}})
                        assert created.is_success, created.text
                        async with asyncio.timeout(15):
                            while True:
                                snapshot = (await http.get("/api/tracker/tasks/echo-via-web")).json()
                                task = snapshot["task"]
                                if task["status"] in {"completed", "failed"}:
                                    break
                                await asyncio.sleep(0.2)
                        assert task["status"] == "completed", task
                        assert task["result_summary"].find("echo through Web Bridge") >= 0
                        assert calls == {"echo through Web Bridge": 1}
                        assert (await http.get("/api/tracker/tasks/old-echo")).json()["task"]["status"] == "queued"
                except Exception as exc:
                    diagnostics = [line for line in log.read_text().splitlines() if "GET /api/tracker/tasks/" not in line]
                    raise AssertionError("\n".join(diagnostics)[-6000:]) from exc
                finally:
                    if web.returncode is None:
                        web.terminate()
                        try:
                            await asyncio.wait_for(web.wait(), 10)
                        except asyncio.TimeoutError:
                            web.kill()
                            await web.wait()
            assert not (tmp_path / ".tcode/job-protocol/web-bridge.pid").exists()

    asyncio.run(check())
