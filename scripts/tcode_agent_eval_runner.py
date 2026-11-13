#!/usr/bin/env python3
"""Run one TCode agent turn and emit a structured evaluation trace as JSON."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any


def _bootstrap_path() -> None:
    tcode_root = Path(__file__).resolve().parent.parent
    if str(tcode_root) not in sys.path:
        sys.path.insert(0, str(tcode_root))


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return _json_safe(usage)
    result = {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }
    cost = getattr(usage, "cost", None)
    if cost is not None:
        result["cost"] = float(cost or 0.0)
    return result


def _tool_call_to_dict(tool_call: Any) -> dict[str, Any]:
    if isinstance(tool_call, dict):
        return _json_safe(tool_call)
    state = getattr(tool_call, "state", None)
    state_value = getattr(state, "value", state)
    return {
        "tool_name": str(getattr(tool_call, "tool_name", "") or ""),
        "call_id": str(getattr(tool_call, "call_id", "") or ""),
        "arguments": _json_safe(getattr(tool_call, "arguments", {}) or {}),
        "state": str(state_value or ""),
        "output": _json_safe(getattr(tool_call, "output", None)),
        "error": _json_safe(getattr(tool_call, "error", None)),
        "metadata": _json_safe(getattr(tool_call, "metadata", {}) or {}),
        "start_time": getattr(tool_call, "start_time", None),
        "end_time": getattr(tool_call, "end_time", None),
        "compacted_at": getattr(tool_call, "compacted_at", None),
    }


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return _json_safe(message)
    usage = _usage_to_dict(getattr(message, "usage", None))
    return {
        "role": str(getattr(message, "role", "") or ""),
        "content": str(getattr(message, "content", "") or ""),
        "id": str(getattr(message, "id", "") or ""),
        "parent_id": getattr(message, "parent_id", None),
        "error": _json_safe(getattr(message, "error", None)),
        "finish": getattr(message, "finish", None),
        "cost": float(getattr(message, "cost", 0) or 0),
        "summary": bool(getattr(message, "summary", False)),
        "compacted_at": getattr(message, "compacted_at", None),
        "usage": usage or None,
        "tool_calls": [
            _tool_call_to_dict(tool_call)
            for tool_call in (getattr(message, "tool_calls", []) or [])
        ],
    }


def _flatten_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """Return one record per native tool call, deduplicated by call id."""
    calls: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for message in messages or []:
        message_id = str(
            message.get("id", "") if isinstance(message, dict) else getattr(message, "id", "")
        )
        message_role = str(
            message.get("role", "")
            if isinstance(message, dict)
            else getattr(message, "role", "")
        )
        raw_calls = (
            message.get("tool_calls", [])
            if isinstance(message, dict)
            else getattr(message, "tool_calls", [])
        )
        for raw_call in raw_calls or []:
            call = _tool_call_to_dict(raw_call)
            call.setdefault("message_id", message_id)
            call.setdefault("message_role", message_role)
            call_id = str(call.get("call_id") or "").strip()
            if call_id and call_id in positions:
                # Tool result messages contain the final state/output for the same call.
                existing = calls[positions[call_id]]
                origin_message_id = existing.get("message_id", "")
                origin_message_role = existing.get("message_role", "")
                calls[positions[call_id]] = {
                    **existing,
                    **call,
                    "message_id": origin_message_id,
                    "message_role": origin_message_role,
                }
                continue
            if call_id:
                positions[call_id] = len(calls)
            calls.append(call)
    return calls


def _collect_artifact_value(value: Any, found: list[str], seen: set[str]) -> None:
    if isinstance(value, (str, Path)):
        path = str(value).strip()
        if path and path not in seen:
            seen.add(path)
            found.append(path)
        return
    if isinstance(value, dict):
        preferred_keys = (
            "host_path",
            "path",
            "output_path",
            "artifact_path",
            "host_artifact_path",
        )
        preferred = [value.get(key) for key in preferred_keys if value.get(key)]
        if preferred:
            for item in preferred:
                _collect_artifact_value(item, found, seen)
        else:
            for item in value.values():
                _collect_artifact_value(item, found, seen)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_artifact_value(item, found, seen)


def _collect_artifact_paths(messages: list[Any]) -> list[str]:
    """Collect explicit artifact paths from tool metadata without guessing from text."""
    found: list[str] = []
    seen: set[str] = set()
    singular_keys = {
        "artifact_path",
        "host_artifact_path",
        "output_path",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).strip().lower()
                if normalized == "artifacts" or normalized in singular_keys:
                    _collect_artifact_value(item, found, seen)
                elif isinstance(item, (dict, list, tuple, set)):
                    visit(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)

    for call in _flatten_tool_calls(messages):
        visit(call.get("metadata") or {})
    return found


def _collect_subagent_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive team routing from native task/assign_task calls and their metadata."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(agent: Any, call: dict[str, Any]) -> None:
        if isinstance(agent, (list, tuple, set)):
            for item in agent:
                add(item, call)
            return
        if isinstance(agent, dict):
            add(agent.get("agent") or agent.get("name") or agent.get("worker_name"), call)
            return
        for name in str(agent or "").split(","):
            normalized = name.strip()
            if not normalized:
                continue
            call_id = str(call.get("call_id") or "")
            key = (normalized, call_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "agent": normalized,
                    "tool_name": str(call.get("tool_name") or ""),
                    "call_id": call_id,
                }
            )

    for call in tool_calls or []:
        tool_name = str(call.get("tool_name") or "").strip().lower()
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
        if tool_name == "task":
            add(metadata.get("subagent"), call)
            add(metadata.get("subagents"), call)
            add(metadata.get("tasks"), call)
            if not metadata.get("subagent") and not metadata.get("subagents"):
                add(arguments.get("subagent_type"), call)
        elif tool_name in {"assign_task", "sessions_spawn"}:
            add(
                arguments.get("worker_name")
                or arguments.get("agent")
                or arguments.get("agentId"),
                call,
            )
    return result


def _runtime_artifact_paths(session_id: str, cwd: str) -> list[str]:
    from tcode_agent.runtime.artifacts import RuntimeArtifactManager

    found: list[str] = []
    seen: set[str] = set()
    for bundle in RuntimeArtifactManager.list_for_session(session_id, cwd):
        _collect_artifact_value(bundle.get("files", []), found, seen)
    return found


def _empty_result(error: str = "") -> dict[str, Any]:
    return {
        "final_output": "",
        "tool_calls": [],
        "subagent_calls": [],
        "usage": {},
        "artifacts": [],
        "messages": [],
        "duration_s": 0.0,
        "error": str(error or ""),
    }


async def _run_once(
    *,
    cwd: str,
    agent_name: str,
    prompt: str,
    model_ref: str = "",
) -> dict[str, Any]:
    from tcode_agent.agent.agent import Agent
    from tcode_agent.agent.agents import BuiltinAgents
    from tcode_agent.config.manager import ConfigManager
    from tcode_agent.mcp.manager_auto import MCPManager
    from tcode_agent.plugins.manager import PluginManager
    from tcode_agent.providers import create_provider

    agent: Any = None
    final_message: Any = None
    error = ""
    session_id = f"eval-{uuid.uuid4().hex[:10]}"
    trace_messages: list[dict[str, Any]] = []
    trace_positions: dict[str, int] = {}

    def capture_messages() -> None:
        if agent is None:
            return
        for message in list(getattr(agent, "messages", []) or []):
            serialized = _message_to_dict(message)
            message_id = str(serialized.get("id") or "").strip()
            key = message_id or f"object:{id(message)}"
            if key in trace_positions:
                trace_messages[trace_positions[key]] = serialized
            else:
                trace_positions[key] = len(trace_messages)
                trace_messages.append(serialized)

    try:
        ConfigManager.clear()
        ConfigManager.load(cwd)
        await PluginManager.get_global(cwd)
        provider = create_provider(
            workdir=cwd,
            model_ref=str(model_ref or "").strip() or None,
        )

        agent_info = BuiltinAgents.get(agent_name)
        if agent_info is None:
            available = ", ".join(sorted(BuiltinAgents.all()))
            raise ValueError(
                f"Unknown eval agent '{agent_name}'. Available agents: {available}"
            )
        agent_cfg = ConfigManager.get_agent(agent_name)
        agent_info = BuiltinAgents.apply_config(agent_info, agent_cfg)
        agent = Agent(
            agent_info=agent_info,
            provider=provider,
            session_id=session_id,
            workdir=cwd,
        )

        if not MCPManager.is_initialized():
            await MCPManager.initialize(workdir=cwd)
        async for message in agent.run_stream(prompt):
            capture_messages()
            if str(getattr(message, "role", "") or "") == "assistant":
                final_message = message
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        capture_messages()
        try:
            await MCPManager.close()
        except Exception as exc:
            close_error = f"MCP close failed: {exc}"
            error = f"{error}; {close_error}" if error else close_error

    messages = trace_messages
    tool_calls = _flatten_tool_calls(messages)
    artifacts = _collect_artifact_paths(messages)
    for path in _runtime_artifact_paths(session_id, cwd):
        if path not in artifacts:
            artifacts.append(path)

    return {
        "final_output": str(getattr(final_message, "content", "") or ""),
        "tool_calls": tool_calls,
        "subagent_calls": _collect_subagent_calls(tool_calls),
        "usage": _usage_to_dict(getattr(agent, "total_usage", None)),
        "artifacts": artifacts,
        "messages": messages,
        "duration_s": 0.0,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one TCode agent turn and print a structured JSON trace.",
    )
    parser.add_argument("--cwd", required=True, help="Working directory for TCode agent")
    parser.add_argument("--agent", default="build", help="Builtin agent name")
    parser.add_argument("--model", default="", help="Optional provider/model override")
    parser.add_argument("--prompt", default="", help="Prompt fallback when stdin is empty")
    args = parser.parse_args()

    _bootstrap_path()
    cwd = os.path.abspath(args.cwd)
    prompt = args.prompt if args.prompt.strip() else sys.stdin.read()
    started = time.monotonic()
    capture_out = io.StringIO()
    capture_err = io.StringIO()

    if not prompt.strip():
        payload = _empty_result("empty prompt")
    else:
        try:
            with contextlib.redirect_stdout(capture_out), contextlib.redirect_stderr(capture_err):
                payload = asyncio.run(
                    _run_once(
                        cwd=cwd,
                        agent_name=args.agent,
                        prompt=prompt,
                        model_ref=args.model,
                    )
                )
        except Exception as exc:
            payload = _empty_result(f"{type(exc).__name__}: {exc}")

    payload["duration_s"] = time.monotonic() - started
    noisy = capture_out.getvalue().strip()
    noisy_err = capture_err.getvalue().strip()
    if noisy:
        print(noisy, file=sys.stderr)
    if noisy_err:
        print(noisy_err, file=sys.stderr)

    sys.stdout.write(json.dumps(_json_safe(payload), ensure_ascii=False))
    return 1 if payload.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
