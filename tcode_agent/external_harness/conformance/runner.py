"""执行第三方 Harness Adapter 的离线或 live conformance。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ...runtime.agent_backend import NormalizedMessage, NormalizedRunInput, RunContext
from ...runtime.interaction_store import InteractionStore
from ...adapters.external_harness import ExternalHarnessBackend
from ..handoff_store import HarnessHandoffStore
from ..process_manager import HarnessProcessManager
from ..compatibility import evaluate_compatibility
from ..provider import HarnessProviderContext
from ..session_binding import HarnessSessionBindingStore
from ..security import render_argv
from .assertions import observed_capabilities, validate_event_stream
from .report import ConformanceCase, HarnessConformanceReport


_UNICODE_MARKER = "UNICODE-你好-🧪"


class _FixtureRegistry:
    def __init__(self, definition: Any) -> None:
        self.definition = definition

    def get(self, runtime_id: str) -> Any:
        return self.definition if runtime_id == self.definition.id else None

    def allowed_for_request(
        self, runtime_id: str, *, public_server: bool
    ) -> tuple[bool, str]:
        return (runtime_id == self.definition.id, "")

    def effective_capabilities(self, runtime_id: str) -> dict[str, object]:
        if runtime_id != self.definition.id:
            return {"available": False}
        return self.definition.capabilities.model_dump(
            mode="json", by_alias=True
        )


def _fixture_script(stream_format: str, *, hang: bool = False) -> str:
    if stream_format == "acp-json-rpc":
        if hang:
            return """import json, sys, time
def send(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
for raw in sys.stdin:
    value = json.loads(raw)
    method = value.get("method", "")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":value["id"],"result":{"protocolVersion":1,"agentCapabilities":{},"agentInfo":{"name":"TCode fixture","version":"1"}}})
    elif method in {"session/new", "session/load"}:
        send({"jsonrpc":"2.0","id":value["id"],"result":{"sessionId":"fixture-session"}})
    elif method == "session/prompt":
        time.sleep(30)
"""
        return """import json, sys
from pathlib import Path
def send(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
state = Path(__file__).with_suffix(".state")
for raw in sys.stdin:
    value = json.loads(raw)
    method = value.get("method", "")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":value["id"],"result":{"protocolVersion":1,"agentCapabilities":{},"agentInfo":{"name":"TCode fixture","version":"1"}}})
    elif method == "session/new":
        if state.exists():
            send({"jsonrpc":"2.0","id":value["id"],"error":{"code":-32000,"message":"second run must load the session"}})
        else:
            state.write_text("created", encoding="utf-8")
            send({"jsonrpc":"2.0","id":value["id"],"result":{"sessionId":"fixture-session"}})
    elif method == "session/load":
        send({"jsonrpc":"2.0","id":value["id"],"result":{"sessionId":"fixture-session"}})
    elif method == "session/prompt":
        text = value["params"]["prompt"][0]["text"]
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"fixture-session","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":text}}}})
        send({"jsonrpc":"2.0","id":value["id"],"result":{"stopReason":"end_turn"}})
"""
    if hang:
        return "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n"
    if stream_format == "plain":
        return "import sys\nsys.stdout.write(sys.stdin.read())\n"
    if stream_format == "codex-jsonl":
        payload = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _UNICODE_MARKER},
        }
    elif stream_format == "claude-stream-json":
        payload = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": _UNICODE_MARKER}]
            },
        }
    else:
        payload = {"delta": _UNICODE_MARKER}
    return (
        "import json, sys\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({payload!r}, ensure_ascii=False), flush=True)\n"
    )


def _transport_fixture_backend(
    definition: Any, directory: Path, *, hang: bool = False
) -> ExternalHarnessBackend:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fixture_harness.py"
    script.write_text(
        _fixture_script(definition.invocation.stream_format, hang=hang),
        encoding="utf-8",
    )
    fixture_definition = definition.model_copy(
        update={
            "executables": definition.executables.model_copy(
                update={"primary": sys.executable, "fallbacks": []}
            ),
            "invocation": definition.invocation.model_copy(
                update={"args": [script.as_posix()]}
            ),
        }
    )
    database = directory / "conformance.sqlite"
    registry = _FixtureRegistry(fixture_definition)
    interaction_store = InteractionStore(database)
    return ExternalHarnessBackend(
        runtime_id=definition.id,
        registry=registry,
        interaction_store=interaction_store,
        process_manager=HarnessProcessManager(),
        binding_store=HarnessSessionBindingStore(database),
        handoff_store=HarnessHandoffStore(database),
    )


async def _verify_generic_cancellation(
    definition: Any,
    directory: Path,
) -> tuple[bool, str]:
    backend = _transport_fixture_backend(
        definition,
        directory / "cancel-fixture",
        hang=True,
    )
    run_id = f"cancel-{uuid.uuid4().hex}"
    run_input = NormalizedRunInput(
        thread_id=f"thread-{uuid.uuid4().hex}",
        run_id=run_id,
        runtime_id=definition.id,
        messages=[
            NormalizedMessage(id="cancel-prompt", role="user", content="wait")
        ],
    )
    context = RunContext(
        tenant_id="conformance",
        user_id="conformance",
        workspace_id="fixture",
        workdir=directory.as_posix(),
        session_id=run_input.thread_id,
        run_id=run_id,
        agent_id="default",
        runtime_id=definition.id,
        request_id=f"request-{uuid.uuid4().hex}",
    )

    async def consume() -> list[Any]:
        return [event async for event in backend.run(run_input, context)]

    task = asyncio.create_task(consume())
    for _ in range(200):
        if await backend.process_manager.is_active(run_id):
            break
        if task.done():
            break
        await asyncio.sleep(0.01)
    if not await backend.process_manager.is_active(run_id):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return False, "cancellation fixture process did not start"
    await backend.cancel(run_id, context)
    try:
        await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True), timeout=7
        )
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return False, "cancel did not stop the process group within 7 seconds"
    stopped = not await backend.process_manager.is_active(run_id)
    return (
        bool(stopped and context.cancelled.is_set()),
        "process group stopped and cancellation state was set"
        if stopped and context.cancelled.is_set()
        else "cancel returned without clearing the active process",
    )


async def run_conformance(
    runtime_id: str,
    *,
    runtime_registry: Any,
    backend_registry: Any,
    live: bool = False,
    workdir: str = "",
) -> HarnessConformanceReport:
    definition = runtime_registry.get(runtime_id)
    record = runtime_registry.get_record(runtime_id)
    cases: list[ConformanceCase] = []
    verified: set[str] = set()
    rejected: set[str] = set()
    if definition is None or record is None:
        return HarnessConformanceReport(
            runtimeId=runtime_id,
            mode="live" if live else "fixture",
            passed=False,
            cases=[
                ConformanceCase(
                    caseId="discovery",
                    passed=False,
                    message="runtime is not loaded; inspect trust and diagnostics",
                )
            ],
        )

    compatibility = evaluate_compatibility(definition)
    cases.append(
        ConformanceCase(
            caseId="schema-version",
            passed=definition.schema_id
            in {"tcode.harness-runtime.v1", "tcode.harness-runtime.v2"},
            message=definition.schema_id,
        )
    )
    cases.append(
        ConformanceCase(
            caseId="definition",
            passed=compatibility.state == "compatible",
            message="; ".join(compatibility.diagnostics),
        )
    )
    allowed, reason = runtime_registry.allowed_for_request(
        runtime_id, public_server=False
    )
    cases.append(
        ConformanceCase(
            caseId="trust-and-enable",
            passed=allowed,
            message=reason,
        )
    )
    try:
        render_argv(
            definition.invocation.args,
            workspace="/tcode/conformance-workspace",
            model_id="provider/model",
        )
        argv_error = ""
    except Exception as exc:
        argv_error = str(exc)
    cases.append(
        ConformanceCase(
            caseId="argv-security",
            passed=not argv_error,
            message=argv_error,
        )
    )
    workspace_safe = (
        definition.security.workspace_mode == "configured"
        and definition.security.inherit_environment == "allowlist"
    )
    cases.append(
        ConformanceCase(
            caseId="workspace-environment",
            passed=workspace_safe,
            message="workspaceMode=configured and inheritEnvironment=allowlist required",
        )
    )
    session_safe = (
        not definition.capabilities.resume
        or definition.session.strategy != "none"
    )
    session_case = ConformanceCase(
        caseId="session-contract",
        passed=session_safe,
        message="resume=true requires a native or ACP session strategy",
    )
    cases.append(session_case)
    public_safe = (
        not definition.security.public_server
        or definition.capabilities.permission_authority in {"tcode", "shared"}
    )
    cases.append(
        ConformanceCase(
            caseId="permission-authority",
            passed=public_safe,
            message="publicServer=true requires tcode/shared permission authority",
        )
    )
    guidance = definition.capabilities.guidance_mode
    guidance_safe = guidance != "restart_turn" or (
        definition.capabilities.cancel and definition.capabilities.resume
    )
    if record.source_type != "builtin" and guidance == "in_turn":
        guidance_safe = False
    cases.append(
        ConformanceCase(
            caseId="guidance-contract",
            passed=guidance_safe,
            message=(
                "restart_turn requires cancel+resume; in_turn is reserved "
                "for the native safe-point contract"
            ),
        )
    )

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if workdir:
        target_workdir = Path(workdir).expanduser().resolve(strict=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="tcode-harness-conformance-")
        target_workdir = Path(temporary.name)
    backend = None
    if live:
        backend = backend_registry.get(runtime_id)
    else:
        provider = runtime_registry.provider_for(runtime_id)
        fixture_factory = getattr(provider, "create_fixture_backend", None)
        if callable(fixture_factory):
            backend = fixture_factory(
                HarnessProviderContext(
                    runtime_registry=runtime_registry,
                    interaction_store=InteractionStore(
                        target_workdir / "provider-fixture.sqlite"
                    ),
                )
            )
        elif record.source_type in {"builtin", "profile"}:
            backend = _transport_fixture_backend(definition, target_workdir)

    if backend is None:
        cases.append(
            ConformanceCase(
                caseId="backend",
                passed=False,
                message=(
                    "runtime backend is unavailable"
                    if live
                    else "Python provider must implement create_fixture_backend()"
                ),
            )
        )
    else:
        cases.append(
            ConformanceCase(
                caseId="backend",
                passed=str(getattr(backend, "backend_id", "")) == runtime_id,
                message="backend_id must equal runtime id",
            )
        )
        try:
            discovered_capabilities = await backend.get_capabilities()
            capabilities_valid = isinstance(discovered_capabilities, dict)
            capabilities_message = ""
        except Exception as exc:
            capabilities_valid = False
            capabilities_message = str(exc)[:1000]
        cases.append(
            ConformanceCase(
                caseId="capability-discovery",
                passed=capabilities_valid,
                message=capabilities_message,
            )
        )
    events: list[Any] = []
    if backend is not None and allowed:
        run_id = f"conformance-{uuid.uuid4().hex}"
        run_input = NormalizedRunInput(
            thread_id=f"thread-{uuid.uuid4().hex}",
            run_id=run_id,
            runtime_id=runtime_id,
            messages=[
                NormalizedMessage(
                    id="unicode-prompt",
                    role="user",
                    content=f"Reply with {_UNICODE_MARKER}",
                )
            ],
        )
        context = RunContext(
            tenant_id="conformance",
            user_id="conformance",
            workspace_id="fixture",
            workdir=target_workdir.as_posix(),
            session_id=run_input.thread_id,
            run_id=run_id,
            agent_id="default",
            runtime_id=runtime_id,
            request_id=f"request-{uuid.uuid4().hex}",
        )
        try:
            async with asyncio.timeout(30 if live else 10):
                events = [event async for event in backend.run(run_input, context)]
            _types, failures = validate_event_stream(events)
            cases.append(
                ConformanceCase(
                    caseId="event-stream",
                    passed=not failures,
                    message="; ".join(failures),
                )
            )
            terminal_count = sum(
                str(getattr(event, "type", "")) == "message.finished"
                for event in events
            )
            cases.append(
                ConformanceCase(
                    caseId="unique-message-terminal",
                    passed=terminal_count == 1,
                    message=f"message.finished count={terminal_count}",
                )
            )
            final_text = "\n".join(
                str(getattr(event, "message", {}).content)
                if hasattr(getattr(event, "message", None), "content")
                else ""
                for event in events
                if str(getattr(event, "type", "")) == "message.finished"
            )
            cases.append(
                ConformanceCase(
                    caseId="unicode",
                    passed=_UNICODE_MARKER in final_text,
                    message="fixture/live backend must preserve Unicode prompt",
                )
            )
            verified.update(observed_capabilities(events))
            if (
                definition.capabilities.resume
                and isinstance(backend, ExternalHarnessBackend)
                and definition.invocation.stream_format == "acp-json-rpc"
            ):
                resume_run_id = f"resume-{uuid.uuid4().hex}"
                resume_input = NormalizedRunInput(
                    thread_id=run_input.thread_id,
                    run_id=resume_run_id,
                    runtime_id=runtime_id,
                    messages=[
                        NormalizedMessage(
                            id="resume-prompt",
                            role="user",
                            content=f"Reply again with {_UNICODE_MARKER}",
                        )
                    ],
                )
                resume_context = RunContext(
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    workspace_id=context.workspace_id,
                    workdir=context.workdir,
                    session_id=context.session_id,
                    run_id=resume_run_id,
                    agent_id=context.agent_id,
                    runtime_id=context.runtime_id,
                    request_id=f"request-{uuid.uuid4().hex}",
                )
                try:
                    async with asyncio.timeout(10):
                        resume_events = [
                            event
                            async for event in backend.run(
                                resume_input, resume_context
                            )
                        ]
                    _resume_types, resume_failures = validate_event_stream(
                        resume_events
                    )
                    if resume_failures:
                        raise RuntimeError("; ".join(resume_failures))
                    verified.add("resume")
                    session_case.message = (
                        "ACP second run loaded the persisted native session"
                    )
                except Exception as exc:
                    session_case.passed = False
                    session_case.message = (
                        "ACP native session resume failed: " + str(exc)[:800]
                    )
        except Exception as exc:
            cases.append(
                ConformanceCase(
                    caseId="event-stream",
                    passed=False,
                    message=str(exc)[:1000],
                )
            )

    cancel_supported = bool(definition.capabilities.cancel)
    cancel_callable = callable(getattr(backend, "cancel", None))
    cancel_passed = not cancel_supported or cancel_callable
    cancel_message = "cancel=true requires an async cancel method"
    if (
        cancel_supported
        and cancel_callable
        and isinstance(backend, ExternalHarnessBackend)
    ):
        try:
            cancel_passed, cancel_message = await _verify_generic_cancellation(
                definition,
                target_workdir,
            )
        except Exception as exc:
            cancel_passed = False
            cancel_message = str(exc)[:1000]
        if cancel_passed:
            verified.add("cancel")
    cases.append(
        ConformanceCase(
            caseId="cancel-contract",
            passed=cancel_passed,
            message=cancel_message,
        )
    )
    for name in ("text", "images", "tools", "resume", "cancel"):
        if name not in verified:
            rejected.add(name)
    if temporary is not None:
        temporary.cleanup()
    return HarnessConformanceReport(
        runtimeId=runtime_id,
        adapterVersion=record.distribution_version,
        definitionDigest=record.definition_digest,
        mode="live" if live else "fixture",
        passed=bool(cases) and all(item.passed for item in cases),
        capabilitiesVerified=sorted(verified),
        capabilitiesRejected=sorted(rejected),
        cases=cases,
    )


__all__ = ["run_conformance"]
