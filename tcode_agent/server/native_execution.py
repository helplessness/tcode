"""Native harness server entry, shared with the Web workflow executor.

Only transport adaptation lives here: session hydration, event delivery and
run lifetime. No plugin-specific routing, prompts or tool policy are allowed.
"""

from __future__ import annotations

import asyncio
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from .state import ServerState
from .agent_selection import normalize_primary_agent_name
from .workflow_dispatch import discover_workflow, prepare_workflow, resume_workflow_route
from ..session.manager import SessionManager
from ..types import Message


def owner_scope(context):
    return {name: getattr(context, name) for name in ("tenant_id", "user_id", "workspace_id")}


def prepare_async_artifacts(persisted, session, agent, run_input):
    """Restore this session's generic artifacts, then import into its snapshot.

    No host mounts or snapshot refresh: preserve the agent's existing changes.
    The existing native copier enforces no-follow, file/byte limits and
    compare-before-replace, so a conflicting local file is never overwritten.
    """
    from ..workspace.manager import AgentWorkspaceManager
    from ..runtime.sandbox.macos_native import copy_tree
    from ..runtime.shared_tracker import SharedTracker
    from ..workspace.async_protein_run import extract_link, materialize_completed_generic_result
    workspace_id = str(session.get('agent_workspace_id') or '')
    control = Path(persisted.workdir).resolve()
    execution = Path(agent.workdir).resolve()
    paths = set()
    goal = (persisted.metadata or {}).get('goal_mode') or {}
    goal_turn = run_input.run_id in goal.get('run_ids', [])
    prompt = run_input.latest_user_text()
    for event in (persisted.metadata or {}).get('async_completion_events', []):
        if not isinstance(event, dict) or event.get('session_id') != persisted.id or event.get('status') != 'completed':
            continue
        root = Path(str(event.get('output_abs_path') or event.get('output_path') or ''))
        if not str(root) or str(root) == '.':
            continue
        if not root.is_absolute():
            root = control / root
        if not ((goal_turn and event.get('origin_request_id') in goal.get('run_ids', []))
                or (event.get('identifier') and event['identifier'] in prompt)
                or (root.is_relative_to(control) and root != control and root.relative_to(control).as_posix() in prompt)):
            continue
        task = SharedTracker.get_task(identifier=event.get('identifier') or '', workdir=str(control)) or {}
        # Protein Run settlement already manages its own input/output boundary.
        if extract_link(task):
            continue
        if not root.is_relative_to(control) or root == control or '..' in root.parts:
            raise ValueError('Async artifact output escapes workspace')
        for raw in event.get('artifacts') or []:
            artifact = Path(str(raw))
            if not artifact.is_absolute():
                relative_to_control = control / artifact
                artifact = relative_to_control if relative_to_control.is_relative_to(root) else root / artifact
            if not artifact.is_relative_to(root) or '..' in artifact.parts:
                raise ValueError('Async artifact escapes its output directory')
            if artifact.is_dir():
                raise ValueError('Async artifact must be a declared regular file')
            if artifact == root / 'mcp_result.json' and not artifact.exists():
                # Older generic completions advertised a file but retained the
                # full protocol payload only in the tracker. Never use previews.
                task_workspace = task.get('workspace') or {}
                if (task.get('session_id') == persisted.id and task.get('status') == 'completed'
                        and str(task_workspace.get('workdir') or '') == str(root)
                        and all(not getattr(persisted, key, None) or task_workspace.get(key) == getattr(persisted, key)
                                for key in ('tenant_id', 'user_id', 'workspace_id'))):
                    materialize_completed_generic_result(control, root, task.get('result_summary') or '')
            if not artifact.exists():
                raise ValueError('Completed async artifact is missing; no complete validated receipt is available')
            paths.add(artifact.relative_to(control).as_posix())
    if not workspace_id:
        return
    workspace = AgentWorkspaceManager.load(control_workdir=persisted.workdir, workspace_id=workspace_id)
    if workspace is None or workspace.strategy != 'snapshot' or not workspace.isolated:
        return
    expected_execution = control / '.tcode' / 'agent-snapshots' / workspace_id
    if (workspace.session_id != persisted.id or workspace.execution_path.resolve() != execution
            or execution != expected_execution or expected_execution.is_symlink()):
        raise ValueError('Async artifact workspace identity mismatch')
    if paths:
        copied = copy_tree(control, execution, relative_paths=paths)
        missing = paths - copied.keys()
        if missing:
            raise ValueError('Completed async artifacts are missing or unavailable in isolation')


def configure_native_agent(agent, session, *, execution_profile=None, reasoning_effort=None):
    from ..runtime.execution_profile import normalize_execution_profile

    profile = normalize_execution_profile(execution_profile or session.get("execution_profile")) or "standard"
    agent.set_session_resource_context(session.get("resource_context") or {})
    agent.set_scheduler_mode(session.get("scheduler_mode", "supervisor"))
    agent.set_execution_profile(profile)
    agent.set_reasoning_effort(reasoning_effort or session.get("reasoning_effort"))
    session["execution_profile"] = profile
    if reasoning_effort:
        session["reasoning_effort"] = reasoning_effort
    return profile


async def open_native_agent(run_input, context):
    runtime = import_module("tcode_agent.server.app")
    from ..config.manager import ConfigManager
    from ..plugins.manager import PluginManager

    ConfigManager.load(context.workdir)
    await PluginManager.get_global(context.workdir)
    persisted = await SessionManager.get(context.session_id, **owner_scope(context))
    if persisted is None:
        raise ValueError("Native session not found")
    if Path(persisted.workdir).resolve() != Path(context.workdir).resolve():
        raise ValueError("Native session workspace mismatch")
    metadata = dict(persisted.metadata or {})
    options = dict(run_input.forwarded_props.get("tcode") or {})
    name = normalize_primary_agent_name(
        persisted.agent_name if run_input.agent_id == "default" else run_input.agent_id
    ) or "build"
    session = ServerState.get_or_create_session(
        context.session_id, agent_name=name, workdir=context.workdir,
        execution_profile=options.get("executionProfile") or metadata.get("execution_profile"),
        scheduler_mode=options.get("schedulerMode") or metadata.get("scheduler_mode"),
    )
    workflow_tasks = session.get("workflow_auto_continue_tasks")
    has_workflow_tasks = isinstance(workflow_tasks, dict) and any(
        task and hasattr(task, "done") and not task.done()
        for task in workflow_tasks.values()
    )
    if has_workflow_tasks or session.get("workflow_auto_continue_task") or session.get("native_gateway_run_id"):
        raise RuntimeError("Native session already has an active run")
    session.update(owner_scope(context))
    if run_input.model_id:
        session["model"] = run_input.model_id
    if metadata.get("config_snapshot_id"):
        session["config_snapshot_id"] = metadata["config_snapshot_id"]
    session["reasoning_effort"] = options.get("reasoningEffort") or metadata.get("reasoning_effort")
    session["resource_context"] = runtime._merge_resource_context(
        metadata.get("resource_context"),
        runtime._build_message_resource_context(
            run_input.latest_user_text(), session_id=context.session_id, workdir=context.workdir,
        ),
    )
    agent = ServerState.get_or_create_agent(context.session_id, hydrate=False)
    agent.messages = list(persisted.messages or [])
    agent._existing_session = persisted
    prepare_async_artifacts(persisted, session, agent, run_input)
    runtime._bind_guidance_v2_to_agent(agent, persisted)
    configure_native_agent(agent, session)
    agent._active_client_request_id = context.request_id
    agent._native_server_session = context.session_id
    metadata.update({
        key: session[key] for key in (
            "config_snapshot_id", "config_fingerprint", "config_policy_fingerprint", "config_summary",
            "execution_profile", "scheduler_mode", "reasoning_effort", "resource_context",
        ) if key in session
    })
    await SessionManager.update(context.session_id, **owner_scope(context), metadata=metadata, agent_name=name)
    await runtime._initialize_chat_turn_runtime(session=session, fallback_workdir=context.workdir)
    return agent, ServerState.providers[context.session_id]


async def run_native_turn(*, agent, run_input, context, question_handler, emit_message, emit_state, discover_workflows=True):
    runtime = import_module("tcode_agent.server.app")
    session = ServerState.sessions[context.session_id]
    workflow_task = None
    admitted_run = None

    async def on_event(payload):
        # Worker transcripts remain in their own sessions, just as in Web.
        if payload.get("type") == "message" and payload.get("session_id", context.session_id) == context.session_id:
            message_id = str(payload.get("message_id") or "")
            if not message_id:
                return
            persisted = await SessionManager.get(context.session_id, **owner_scope(context))
            message = next((m for m in (getattr(persisted, "messages", None) or []) if m.id == message_id), None)
            await emit_message(message or Message(role="assistant", content=str(payload.get("content") or ""), id=message_id))
        elif payload.get("type") == "workflow_auto_route" or payload.get("workflow_status"):
            await emit_state({"tcode": {"workflow": payload}})

    unsubscribe = ServerState.subscribe_session_events(context.session_id, on_event)
    session["native_gateway_run_id"] = context.run_id
    session["question_handler"] = question_handler
    try:
        decision = await discover_workflow(
            workdir=context.workdir, session_id=context.session_id,
            message=run_input.latest_user_text(), requested_agent_name=session["agent_name"],
            resource_context=session.get("resource_context") or {},
        ) if discover_workflows else None
        decision = resume_workflow_route(
            decision=decision, session_id=context.session_id, workdir=context.workdir,
            message=run_input.latest_user_text(), requested_agent_name=session["agent_name"],
        ) if discover_workflows else None
        admission = await prepare_workflow(
            session_id=context.session_id, workdir=context.workdir,
            message=run_input.latest_user_text(), decision=decision, agent=agent,
            owner_kwargs=owner_scope(context), question_handler=question_handler,
        ) if discover_workflows else SimpleNamespace(run=None, declined_decision=None)
        if admission.run is not None:
            admitted_run = admission.run
            latest_user = next((m for m in reversed(run_input.messages) if m.role == "user"), None)
            handled = await runtime._attempt_server_side_workflow_auto_route(
                session_id=context.session_id, workdir=context.workdir,
                message=run_input.latest_user_text(), decision=admission.decision, run=admission.run,
                resource_context=session.get("resource_context") or {},
                question_handler=question_handler, send_event=on_event,
                user_message_id=latest_user.id if latest_user else "",
            )
            if not handled:
                raise RuntimeError("Native workflow dispatch failed; refusing an alternate agent execution")
            workflow_task = session.get("workflow_auto_continue_task")
            if isinstance(workflow_task, asyncio.Task):
                # Do not end AG-UI before the shared executor's semantic acceptance
                # and final result. Await also propagates client cancellation.
                await workflow_task
            return
        if admission.declined_decision is not None:
            runtime._inject_workflow_declined_direct_instruction(agent, decision=admission.declined_decision)
        async for message in agent.run_stream(run_input.latest_user_text()):
            await emit_message(message)
    except asyncio.CancelledError:
        if admitted_run is not None:
            runtime.WorkflowRunManager.update_status(
                admitted_run.run_id, status="cancelled", workdir=context.workdir,
                metadata={"cancelled_by": "native_gateway", "cancel_reason": "run_cancelled"},
            )
        raise
    finally:
        unsubscribe()
        if workflow_task is None and admitted_run is not None:
            workflow_task = session.get("workflow_auto_continue_task")
        if session.get("native_gateway_run_id") == context.run_id:
            session.pop("native_gateway_run_id", None)
            session.pop("question_handler", None)
        if isinstance(workflow_task, asyncio.Task) and not workflow_task.done():
            workflow_task.cancel()
            await asyncio.gather(workflow_task, return_exceptions=True)
