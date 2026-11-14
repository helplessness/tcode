from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock

import pytest

from tcode_agent.mcp._proxy import _simplified_submit_args
from tcode_agent.research import boltz_rounds
from tcode_agent.research.experiment_store import ResearchExperimentStore
from tcode_agent.tools.base import ToolContext
from tcode_agent.workspace.async_protein_run import ensure_for_submission, settle_result
from tcode_agent.workspace.input_resources import NATIVE_INPUT_FILES


def test_desktop_fixture_does_not_discover_private_executor_configs(monkeypatch, tmp_path):
    from tcode_agent.mcp import executor_config

    monkeypatch.setenv("TCODE_DESKTOP_TEST_INSTANCE", "1")
    monkeypatch.setenv("TCODE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.delenv("TCODE_EXECUTOR_MCP_CONFIG", raising=False)
    monkeypatch.delenv("TCODE_EXECUTOR_MCP_CONFIGS", raising=False)
    candidates = executor_config.executor_config_candidates(workdir=tmp_path)
    assert candidates
    assert all(path.parent == tmp_path for path in candidates)


def test_boltz_rounds_survive_reload_and_keep_original(tmp_path, monkeypatch):
    context = ToolContext("chat", "message", "build", None, extra={
        "workdir": str(tmp_path), "tenant_id": "tenant", "user_id": "user", "workspace_id": "workspace",
        "boltz_research": {"research_experiment_id": "experiment"},
    })
    def experiment(experiment_id, **owner):
        if experiment_id != "experiment" or owner["workspace_id"] != "workspace":
            raise LookupError("experiment not found")
        return SimpleNamespace(frozen_at="")

    monkeypatch.setattr(ResearchExperimentStore, "get_experiment", experiment)
    monkeypatch.delenv("TCODE_PROTEIN_RUNS_ROOT", raising=False)
    original = tmp_path / "first.yaml"
    replacement = tmp_path / "second.yaml"
    original.write_text("first")
    replacement.write_text("second")
    server, tool = boltz_rounds.BOLTZ
    context._mcp_file_contracts = {(server, tool): {"files": list(NATIVE_INPUT_FILES[(server, tool)])}}
    link = ensure_for_submission(context=context, identifier="LOCAL-first", server_name=server,
                                 tool_name=tool, arguments={"tools_input": str(original), "diffusion_samples": 2})
    assert settle_result(link, identifier="LOCAL-first", status="completed",
                         target_server=server, target_tool=tool, result_output='{"completed":true}')
    first, = boltz_rounds.rounds_for(tmp_path, ["experiment"])["experiment"]
    assert first["run_id"] == link["run_id"]
    assert first["outputs"]
    with pytest.raises(ValueError, match="上一轮"):
        boltz_rounds.next_round(context, "other", link["run_id"], str(replacement))
    new_args = boltz_rounds.next_round(context, "experiment", link["run_id"], str(replacement))
    assert new_args == {"tools_input": str(replacement), "diffusion_samples": 2}
    context.extra["boltz_research"]["source_protein_run_id"] = link["run_id"]
    next_link = ensure_for_submission(context=context, identifier="LOCAL-second", server_name=server,
                                      tool_name=tool, arguments=new_args)
    assert next_link["run_id"] != link["run_id"]
    rows = boltz_rounds.rounds_for(tmp_path, ["experiment"])["experiment"]
    assert {row["run_id"]: row["source_run_id"] for row in rows} == {
        link["run_id"]: "", next_link["run_id"]: link["run_id"],
    }
    assert original.read_text() == "first"


def test_research_metadata_is_not_forwarded_as_boltz_tool_arguments():
    server, tool = boltz_rounds.BOLTZ
    parsed, error = _simplified_submit_args({"input_config": {
        "target_server": server, "target_tool": tool, "tools_input": "second.yaml",
        "_tcode_research_experiment": "experiment", "_tcode_boltz_from_run": "previous",
    }})
    assert error is None
    assert parsed["target_args"] == {"tools_input": "second.yaml"}
    assert parsed["research_experiment_id"] == "experiment"
    assert parsed["source_protein_run_id"] == "previous"


def test_chat_explicit_association_enters_existing_submission_path(monkeypatch, tmp_path):
    from tcode_agent.mcp import _proxy, custom_targets
    from tcode_agent.types import ToolResult

    monkeypatch.setattr(ResearchExperimentStore, "get_experiment", lambda *a, **kw: SimpleNamespace(frozen_at=""))
    monkeypatch.setattr(_proxy, "_get_agent_allowed_routed_servers", lambda context: None)
    monkeypatch.setattr(_proxy, "_validate_target_tool", AsyncMock(return_value=None))
    monkeypatch.setattr(custom_targets, "resolve", lambda server, context: server)
    monkeypatch.setattr(custom_targets, "configured_alias", lambda server, context: server)
    monkeypatch.setattr(custom_targets, "authorize_inputs", AsyncMock(return_value=True))
    monkeypatch.setattr(custom_targets, "pinned_target", lambda *args: None)
    submit = AsyncMock(return_value=ToolResult(title="mock", output="queued"))
    monkeypatch.setattr(_proxy, "_submit_authorized_job", submit)
    context = ToolContext("chat", "message", "build", None, extra={
        "workdir": str(tmp_path), "tenant_id": "tenant", "user_id": "user", "workspace_id": "workspace",
    })
    server, tool = boltz_rounds.BOLTZ
    base = {"target_server": server, "target_tool": tool, "tools_input": "input.yaml"}
    asyncio.run(_proxy._proxy_submit_job(None, {"input_config": {
        **base, "_tcode_research_experiment": "experiment",
    }}, context))
    call = submit.await_args.kwargs
    assert call["context"].extra["boltz_research"]["research_experiment_id"] == "experiment"
    assert call["tool_args"] == {"input_config": {"tools_input": "input.yaml"}}
    asyncio.run(_proxy._proxy_submit_job(None, {"input_config": base}, context))
    assert "boltz_research" not in submit.await_args.kwargs["context"].extra


def test_approved_review_retains_boltz_experiment_at_queue_boundary(monkeypatch, tmp_path):
    from tcode_agent.mcp import _symphony, artifact_boundary
    from tcode_agent.runtime import async_watcher
    from tcode_agent.server._shared import _enqueue_approved_proxy_review

    queue = AsyncMock(return_value={"identifier": "LOCAL-fixture"})
    monkeypatch.setattr(_symphony, "enqueue_symphony_local_issue", queue)
    monkeypatch.setattr(async_watcher, "register", lambda **kwargs: None)
    monkeypatch.setattr(async_watcher, "start_watcher", AsyncMock())
    args = {"input_config": {"tools_input": "fixture.yaml", "diffusion_samples": 2}}
    monkeypatch.setattr(artifact_boundary, "prepare_mcp_artifact_arguments", AsyncMock(return_value=(args, [])))
    server, tool = boltz_rounds.BOLTZ
    result = asyncio.run(_enqueue_approved_proxy_review({
        "id": "review-fixture", "tool_server": server, "tool_name": tool, "tool_args": args,
        "review_data": {"proxy_source": "job_protocol_submit", "boltz_research": {
            "research_experiment_id": "experiment", "source_protein_run_id": "previous",
        }},
    }, session_id="chat", agent_name="build", workdir=str(tmp_path),
        owner={"tenant_id": "tenant", "user_id": "user", "workspace_id": "workspace"}))
    assert result == {"identifier": "LOCAL-fixture"}
    queue.assert_awaited_once()
    call = queue.await_args.kwargs
    assert call["normalized_args"] == args
    assert call["context"].extra["boltz_research"] == {
        "research_experiment_id": "experiment", "source_protein_run_id": "previous",
    }
