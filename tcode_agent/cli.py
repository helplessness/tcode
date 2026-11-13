#!/usr/bin/env python3
"""Interactive multi-turn CLI for TCode agents."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .cli_question import TerminalQuestionHandler, terminal_question_handler
from .runtime.question_policy import normalize_question_policy
from .runtime.env_file import load_process_env_file


@dataclass
class _FinalReply:
    content: str = ""
    error: str = ""


@contextlib.contextmanager
def _suppress_console_output() -> Iterator[None]:
    """Silence progress without buffering it; keep existing file logs intact."""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        saved_fds: list[tuple[int, int]] = []
        try:
            for stream in (sys.stdout, sys.stderr):
                stream.flush()
            # Include native libraries, inherited child output, and logging
            # handlers that captured a console stream before this context.
            for fd in (1, 2):
                saved_fd = os.dup(fd)
                saved_fds.append((fd, saved_fd))
                os.dup2(sink.fileno(), fd)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
        finally:
            sink.flush()
            for fd, saved_fd in reversed(saved_fds):
                try:
                    os.dup2(saved_fd, fd)
                finally:
                    os.close(saved_fd)


def _bootstrap_path() -> None:
    """保留源码脚本调用接口；安装后的入口不修改 Python 搜索路径。"""


def _load_env_file(env_file: str) -> None:
    if not env_file:
        return
    try:
        load_process_env_file(env_file)
    except Exception:
        return


def _default_env_file(workdir: str) -> str:
    configured_root = str(os.environ.get("TCODE_CONFIG_ROOT", "") or "").strip()
    if configured_root:
        return os.path.join(
            os.path.abspath(os.path.expanduser(configured_root)),
            "config",
            "symphony_route.env",
        )
    return os.path.join(workdir, "config", "symphony_route.env")


def _default_config_root(workdir: str) -> str:
    """Prefer initialized private config when running the source CLI."""
    source_root = Path(__file__).resolve().parent.parent
    candidates = [
        Path(workdir) / ".tcode-local",
        source_root / ".tcode-local",
    ]
    for candidate in candidates:
        if (candidate / "tcode.json").is_file():
            return candidate.resolve().as_posix()
    # Installed/projectless usage keeps the documented directory-search
    # behavior and can still pin an external configuration explicitly.
    return os.path.abspath(workdir)


def _configure_environment(workdir: str) -> str:
    cwd = os.path.abspath(workdir or os.getcwd())
    if not os.path.isdir(cwd):
        raise FileNotFoundError(f"工作目录不存在: {cwd}")
    os.environ["TCODE_WORKDIR"] = cwd
    os.environ.setdefault("TCODE_CONFIG_ROOT", _default_config_root(cwd))
    env_file = os.environ.get("TCODE_ENV_FILE") or _default_env_file(cwd)
    _load_env_file(env_file)
    return cwd


def _shorten(text: str, limit: int = 80) -> str:
    value = " ".join(str(text or "").strip().split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _format_ts(timestamp: float) -> str:
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp)))


def _print_agent_reply(agent_name: str, content: Any) -> None:
    """Render the final reply as a distinct block, separate from progress logs."""
    title = f"🤖 Agent 回复 · {str(agent_name or 'agent').strip() or 'agent'}"
    border = "─" * max(24, len(title) + 4)
    body = str(content or "")
    print(f"\n╭─ {title}")
    print(f"├{border}")
    print(body, end="" if body.endswith("\n") else "\n")
    print(f"╰{border}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive multi-turn CLI for TCode agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --workdir /path/to/project
  %(prog)s --workdir /path/to/project --agent paper_generalist
  %(prog)s --workdir /path/to/project --agent paper_auto_coordinator --model alibaba-cn/kimi-k2.5
  %(prog)s --workdir /path/to/project --session-id abc123
  %(prog)s --workdir /path/to/project --prompt "总结当前项目" --once
  %(prog)s --workdir /path/to/project --prompt "总结当前项目" --result-only
        """,
    )
    parser.add_argument(
        "--cwd",
        "--workdir",
        dest="workdir",
        default=os.getcwd(),
        help="Agent working directory (default: current directory)",
    )
    parser.add_argument(
        "--agent",
        default="",
        help="Agent name. Supports built-in agents and plugin-registered custom agents.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Optional model override in provider/model format, e.g. alibaba-cn/kimi-k2.5.",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Session ID to create or resume. If omitted, a new session is created.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Require --session-id to already exist.",
    )
    parser.add_argument(
        "--title",
        default="",
        help="Optional session title used when creating a new session.",
    )
    parser.add_argument(
        "--execution-profile",
        default=None,
        help="Optional execution profile, e.g. paper-review / research-web / bio-safe.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="Optional reasoning effort, e.g. minimal / low / medium / high.",
    )
    parser.add_argument(
        "--scheduler-mode",
        default=None,
        help="Optional scheduler mode, e.g. supervisor / chain / swarm / auto_swarm_builder.",
    )
    parser.add_argument(
        "--prompt",
        default="",
        help="Optional first prompt. If stdin is piped, stdin is used when this is empty.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run only one turn, then exit.",
    )
    parser.add_argument(
        "--result-only",
        action="store_true",
        help="Run one turn and print only the final reply to stdout (implies --once). "
        "Suppress progress; questions and failures use stderr.",
    )
    parser.add_argument(
        "--question-policy",
        choices=("ask", "auto-confirm", "skip"),
        default="ask",
        help="Question handling: ask (default) or auto-confirm affirmative confirmations "
        "without interaction. skip is an alias for auto-confirm; tool permissions are unchanged.",
    )
    parser.add_argument(
        "--show-mcp-logs",
        action="store_true",
        help="Show detailed MCP/tool initialization logs. Hidden by default in normal CLI output.",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List recent sessions for the current workdir and exit.",
    )
    parser.add_argument(
        "--list-agents",
        action="store_true",
        help="List available agents for the current workdir and exit.",
    )
    return parser


def _enable_readline_history(workdir: str) -> None:
    try:
        import readline  # type: ignore
    except Exception:
        return

    history_path = Path(workdir) / ".tcode" / "cli_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(history_path.as_posix())
    except Exception:
        pass

    import atexit

    def _save_history() -> None:
        try:
            readline.write_history_file(history_path.as_posix())
        except Exception:
            pass

    atexit.register(_save_history)


async def _close_provider(provider: Any) -> None:
    if provider is None:
        return
    close_method = getattr(provider, "close", None)
    if not close_method:
        return
    try:
        maybe_coro = close_method()
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro
    except Exception:
        return


async def _ensure_config_loaded(workdir: str) -> None:
    from tcode_agent.config.manager import ConfigManager

    ConfigManager.clear()
    ConfigManager.load(workdir)


async def _ensure_plugins_loaded(workdir: str) -> None:
    from tcode_agent.plugins.manager import PluginManager

    await PluginManager.get_global(workdir)


def _resolve_agent_info(requested_name: str, workdir: str) -> tuple[Any, str, bool]:
    from tcode_agent.agent.agents import BuiltinAgents
    from tcode_agent.config.manager import ConfigManager

    requested = str(requested_name or "").strip()
    default_name = ConfigManager.get_default_agent() or "build"
    selector = requested or default_name
    initial_plan_mode = selector == "plan"

    effective_name = selector
    if initial_plan_mode:
        effective_name = default_name if default_name != "plan" else "build"

    agent_info = BuiltinAgents.get(effective_name)
    if not agent_info:
        raise ValueError(f"未知 agent: {requested_name or selector or effective_name}")

    agent_config = ConfigManager.get_agent(effective_name)
    mcp_agent_config = BuiltinAgents.get_agent_mcp_config(effective_name, workdir)
    merged_agent_config: dict[str, Any] = {}
    if isinstance(agent_config, dict):
        merged_agent_config.update(agent_config)
    if isinstance(mcp_agent_config, dict):
        merged_agent_config.update(mcp_agent_config)
    if merged_agent_config:
        agent_info = BuiltinAgents.apply_config(agent_info, merged_agent_config)

    return agent_info, selector or agent_info.name, initial_plan_mode


async def _persist_session(
    session: Any,
    agent: Any,
    *,
    last_user_message: str = "",
    mark_status: Optional[str] = None,
) -> Any:
    from tcode_agent.session.manager import SessionManager

    metadata = dict(getattr(session, "metadata", {}) or {})
    metadata["cli_chat"] = True
    metadata["cli_requested_agent"] = str(
        metadata.get("cli_requested_agent") or agent.agent_info.name
    ).strip() or agent.agent_info.name
    if getattr(agent, "_execution_profile", None):
        metadata["execution_profile"] = agent._execution_profile
    if getattr(agent, "_reasoning_effort", None):
        metadata["reasoning_effort"] = agent._reasoning_effort
    if getattr(agent, "_scheduler_mode", None):
        metadata["scheduler_mode"] = agent._scheduler_mode

    title = str(getattr(session, "title", "") or "").strip()
    if not title and last_user_message.strip():
        title = _shorten(last_user_message, 60)

    await SessionManager.update(
        session.id,
        title=title,
        agent_name=agent.agent_info.name,
        workdir=agent.workdir,
        metadata=metadata,
        messages=list(agent.messages),
        status=mark_status or "active",
        last_active_at=time.time(),
    )
    refreshed = await SessionManager.get(session.id)
    return refreshed or session


async def _open_agent_session(
    *,
    workdir: str,
    session_id: str,
    requested_agent_name: str,
    model_ref: str,
    title: str,
    execution_profile: Optional[str],
    reasoning_effort: Optional[str],
    scheduler_mode: Optional[str],
    resume_required: bool,
    question_handler: Optional[TerminalQuestionHandler] = None,
    question_policy: str = "ask",
) -> tuple[Any, Any, str, Any]:
    from tcode_agent.agent.agent import Agent
    from tcode_agent.providers import create_provider
    from tcode_agent.session.manager import SessionManager

    question_policy = normalize_question_policy(question_policy)
    existing_session = None
    normalized_session_id = str(session_id or "").strip()
    if normalized_session_id:
        existing_session = await SessionManager.get(normalized_session_id)
        if resume_required and existing_session is None:
            raise ValueError(f"会话不存在: {normalized_session_id}")

    if existing_session and existing_session.workdir:
        existing_workdir = os.path.abspath(str(existing_session.workdir))
        if existing_workdir != os.path.abspath(workdir):
            raise ValueError(
                f"会话 {existing_session.id} 属于另一个 workdir: {existing_workdir}"
            )

    preferred_agent_name = str(requested_agent_name or "").strip()
    if not preferred_agent_name and existing_session is not None:
        metadata = dict(existing_session.metadata or {})
        preferred_agent_name = str(
            metadata.get("cli_requested_agent") or existing_session.agent_name or ""
        ).strip()

    try:
        agent_info, selector, initial_plan_mode = _resolve_agent_info(
            preferred_agent_name,
            workdir,
        )
    except ValueError:
        await _ensure_plugins_loaded(workdir)
        agent_info, selector, initial_plan_mode = _resolve_agent_info(
            preferred_agent_name,
            workdir,
        )

    metadata = dict(existing_session.metadata or {}) if existing_session else {}
    effective_model_ref = str(model_ref or "").strip()
    if not effective_model_ref:
        effective_model_ref = str(metadata.get("cli_model_ref") or "").strip()
    metadata["cli_chat"] = True
    metadata["cli_requested_agent"] = selector
    if effective_model_ref:
        metadata["cli_model_ref"] = effective_model_ref
    if execution_profile is not None:
        metadata["execution_profile"] = execution_profile
    if reasoning_effort is not None:
        metadata["reasoning_effort"] = reasoning_effort
    if scheduler_mode is not None:
        metadata["scheduler_mode"] = scheduler_mode
    if initial_plan_mode and not bool(metadata.get("plan_mode")):
        metadata["plan_mode"] = True
        metadata["plan_mode_source"] = agent_info.name or "build"

    title_text = str(title or "").strip()
    if existing_session is None:
        target_session_id = normalized_session_id or str(uuid.uuid4())
        existing_session = await SessionManager.create(
            session_id=target_session_id,
            title=title_text,
            agent_name=agent_info.name,
            workdir=workdir,
            metadata=metadata,
        )
    else:
        await SessionManager.update(
            existing_session.id,
            title=title_text or existing_session.title,
            agent_name=agent_info.name,
            workdir=workdir,
            metadata=metadata,
            status="active",
            last_active_at=time.time(),
        )
        existing_session = await SessionManager.get(existing_session.id)

    provider = create_provider(
        workdir=workdir,
        model_ref=effective_model_ref or None,
    )
    agent = Agent(
        agent_info=agent_info,
        provider=provider,
        session_id=existing_session.id,
        workdir=workdir,
        existing_session=existing_session,
        question_policy=question_policy,
    )
    agent.set_question_handler(question_handler or TerminalQuestionHandler(sys.stdin, sys.stderr))

    effective_profile = str(metadata.get("execution_profile") or "").strip() or None
    effective_reasoning = str(metadata.get("reasoning_effort") or "").strip() or None
    effective_scheduler = str(metadata.get("scheduler_mode") or "").strip() or None

    if effective_profile:
        agent.set_execution_profile(effective_profile)
    if effective_reasoning:
        agent.set_reasoning_effort(effective_reasoning)
    if effective_scheduler:
        agent.set_scheduler_mode(effective_scheduler)

    if initial_plan_mode and not bool((existing_session.metadata or {}).get("plan_mode")):
        agent._plan_mode = True
        agent._plan_mode_source = agent.agent_info.name or "build"

    return agent, existing_session, selector, provider


async def _list_sessions(workdir: str) -> None:
    from tcode_agent.session.manager import SessionManager

    sessions = await SessionManager.list()
    if not sessions:
        print("当前 workdir 下还没有会话。")
        return

    print(f"工作目录: {workdir}")
    print("最近会话:")
    for session in sorted(sessions, key=lambda item: item.last_active_at, reverse=True)[:20]:
        title = _shorten(session.title or "(无标题)", 36)
        print(
            f"- {session.id} | agent={session.agent_name} | status={session.status} | "
            f"updated={_format_ts(session.last_active_at)} | title={title}"
        )


async def _list_agents(workdir: str) -> None:
    from tcode_agent.agent.agents import BuiltinAgents

    agents = [
        item for item in BuiltinAgents.list() if not bool(getattr(item, "hidden", False))
    ]
    if not agents:
        print("当前没有可用 agent。")
        return

    print(f"工作目录: {workdir}")
    print("可用 agents:")
    for item in sorted(agents, key=lambda agent: agent.name):
        description = _shorten(item.description or "", 70)
        print(f"- {item.name} ({item.mode.value}) {description}")


def _print_welcome(
    *,
    workdir: str,
    agent: Any,
    session: Any,
    selector: str,
    model_ref: str,
) -> None:
    print("=" * 72)
    print("TCode CLI Chat")
    print("=" * 72)
    print(f"workdir : {workdir}")
    print(f"agent   : {agent.agent_info.name}")
    if model_ref:
        print(f"model   : {model_ref}")
    if selector != agent.agent_info.name:
        print(f"selector: {selector}")
    print(f"session : {session.id}")
    if getattr(session, "title", ""):
        print(f"title   : {session.title}")
    print("输入 /help 查看命令，输入 /exit 退出。")
    print("=" * 72)


def _print_help() -> None:
    print(
        "\n".join(
            [
                "可用命令：",
                "  /help                 显示帮助",
                "  /exit 或 /quit        退出 CLI",
                "  /session              显示当前会话信息",
                "  /sessions [limit]     列出最近会话",
                "  /agents               列出可用 agents",
                "  /new [agent]          新建会话，可选切换到另一个 agent",
                "  /resume <session_id>  恢复已有会话",
                "  /title <text>         修改当前会话标题",
                "",
                "直接输入普通文本即可向当前 agent 发送消息。",
            ]
        )
    )


async def _handle_command(
    raw: str,
    *,
    workdir: str,
    state: dict[str, Any],
) -> bool:
    from tcode_agent.session.manager import SessionManager

    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        print(f"命令解析失败: {exc}")
        return True

    if not parts:
        return True

    command = parts[0].lower()
    if command in {"/exit", "/quit"}:
        state["should_exit"] = True
        return False

    if command == "/help":
        _print_help()
        return True

    if command == "/session":
        session = state["session"]
        agent = state["agent"]
        selector = state["selector"]
        print(f"session : {session.id}")
        print(f"agent   : {agent.agent_info.name}")
        model_ref = str(state.get("model_ref") or "").strip()
        if model_ref:
            print(f"model   : {model_ref}")
        if selector != agent.agent_info.name:
            print(f"selector: {selector}")
        print(f"title   : {session.title or '(无标题)'}")
        print(f"status  : {session.status}")
        print(f"updated : {_format_ts(session.last_active_at)}")
        print(f"messages: {len(getattr(session, 'messages', []) or [])}")
        return True

    if command == "/sessions":
        limit = 20
        if len(parts) > 1:
            try:
                limit = max(1, min(100, int(parts[1])))
            except ValueError:
                print("用法: /sessions [limit]")
                return True
        sessions = await SessionManager.list()
        if not sessions:
            print("当前没有会话。")
            return True
        for session in sorted(sessions, key=lambda item: item.last_active_at, reverse=True)[:limit]:
            print(
                f"- {session.id} | agent={session.agent_name} | "
                f"updated={_format_ts(session.last_active_at)} | "
                f"title={_shorten(session.title or '(无标题)', 36)}"
            )
        return True

    if command == "/agents":
        await _list_agents(workdir)
        return True

    if command == "/title":
        if len(parts) < 2:
            print("用法: /title <text>")
            return True
        session = state["session"]
        await SessionManager.update(
            session.id,
            title=" ".join(parts[1:]).strip(),
            last_active_at=time.time(),
        )
        refreshed = await SessionManager.get(session.id)
        state["session"] = refreshed or session
        print(f"已更新标题: {state['session'].title}")
        return True

    if command == "/new":
        requested_agent = parts[1].strip() if len(parts) > 1 else state["selector"]
        old_provider = state["provider"]
        old_session = state["session"]
        await SessionManager.update(
            old_session.id,
            status="idle",
            last_active_at=time.time(),
        )
        agent, session, selector, provider = await _open_agent_session(
            workdir=workdir,
            session_id="",
            requested_agent_name=requested_agent,
            model_ref=state["model_ref"],
            title="",
            execution_profile=state["execution_profile"],
            reasoning_effort=state["reasoning_effort"],
            scheduler_mode=state["scheduler_mode"],
            resume_required=False,
            question_handler=state.get("question_handler"),
            question_policy=state.get("question_policy", "ask"),
        )
        await _close_provider(old_provider)
        state.update(
            {
                "agent": agent,
                "session": session,
                "selector": selector,
                "provider": provider,
                "model_ref": str(session.metadata.get("cli_model_ref") or "").strip(),
                "execution_profile": getattr(agent, "_execution_profile", None),
                "reasoning_effort": getattr(agent, "_reasoning_effort", None),
                "scheduler_mode": getattr(agent, "_scheduler_mode", None),
            }
        )
        print(f"已创建新会话: {session.id} (agent={agent.agent_info.name})")
        return True

    if command == "/resume":
        if len(parts) < 2:
            print("用法: /resume <session_id>")
            return True
        resume_session_id = parts[1].strip()
        old_provider = state["provider"]
        old_session = state["session"]
        await SessionManager.update(
            old_session.id,
            status="idle",
            last_active_at=time.time(),
        )
        agent, session, selector, provider = await _open_agent_session(
            workdir=workdir,
            session_id=resume_session_id,
            requested_agent_name="",
            model_ref="",
            title="",
            execution_profile=None,
            reasoning_effort=None,
            scheduler_mode=None,
            resume_required=True,
            question_handler=state.get("question_handler"),
            question_policy=state.get("question_policy", "ask"),
        )
        await _close_provider(old_provider)
        state.update(
            {
                "agent": agent,
                "session": session,
                "selector": selector,
                "provider": provider,
                "model_ref": str(session.metadata.get("cli_model_ref") or "").strip(),
                "execution_profile": getattr(agent, "_execution_profile", None),
                "reasoning_effort": getattr(agent, "_reasoning_effort", None),
                "scheduler_mode": getattr(agent, "_scheduler_mode", None),
            }
        )
        print(
            f"已恢复会话: {session.id} (agent={agent.agent_info.name}, "
            f"messages={len(agent.messages)})"
        )
        return True

    print(f"未知命令: {raw}")
    print("输入 /help 查看可用命令。")
    return True


async def _interactive_loop(
    *,
    workdir: str,
    state: dict[str, Any],
    initial_prompt: str,
    once: bool,
    final_reply: Optional[_FinalReply] = None,
) -> int:
    if initial_prompt.strip():
        print(f"\n你> {initial_prompt.strip()}\n")
        try:
            result = await state["agent"].run(initial_prompt)
            _print_agent_reply(state["agent"].agent_info.name, result.content)
            state["session"] = await _persist_session(
                state["session"],
                state["agent"],
                last_user_message=initial_prompt,
            )
            if final_reply is not None:
                final_reply.content = str(result.content or "")
        except Exception as exc:
            state["session"] = await _persist_session(
                state["session"],
                state["agent"],
                last_user_message=initial_prompt,
            )
            print(f"\n运行失败: {exc}\n")
            if final_reply is not None:
                final_reply.error = f"运行失败: {exc}"
            return 1
        if once:
            state["session"] = await _persist_session(
                state["session"],
                state["agent"],
                mark_status="idle",
            )
            return 0

    if once:
        error = "`--once` / `--result-only` 需要配合 `--prompt` 或管道输入一起使用。"
        print(error)
        if final_reply is not None:
            final_reply.error = error
        return 2

    _enable_readline_history(workdir)
    _print_welcome(
        workdir=workdir,
        agent=state["agent"],
        session=state["session"],
        selector=state["selector"],
        model_ref=str(state.get("model_ref") or "").strip(),
    )

    while True:
        try:
            raw = input("\n你> ").strip()
        except KeyboardInterrupt:
            print("\n输入 /exit 退出，或继续输入下一条消息。")
            continue
        except EOFError:
            print("\n收到 EOF，正在退出。")
            break

        if not raw:
            continue

        if raw.startswith("/"):
            keep_running = await _handle_command(raw, workdir=workdir, state=state)
            if not keep_running or state.get("should_exit"):
                break
            continue

        try:
            result = await state["agent"].run(raw)
            _print_agent_reply(state["agent"].agent_info.name, result.content)
            state["session"] = await _persist_session(
                state["session"],
                state["agent"],
                last_user_message=raw,
            )
        except Exception as exc:
            state["session"] = await _persist_session(
                state["session"],
                state["agent"],
                last_user_message=raw,
            )
            print(f"\n运行失败: {exc}\n")

    state["session"] = await _persist_session(
        state["session"],
        state["agent"],
        mark_status="idle",
    )
    return 0


async def _main_async(
    args: argparse.Namespace, *, final_reply: Optional[_FinalReply] = None,
    question_handler: Optional[TerminalQuestionHandler] = None,
) -> int:
    """加载配置与插件后执行一次性或交互式 CLI 会话。"""

    from tcode_agent.mcp.manager_auto import MCPManager

    workdir = _configure_environment(args.workdir)
    # Keep normal CLI output focused on the model call and its reply. The
    # manager remains verbose for non-CLI callers unless they opt out too.
    os.environ["TCODE_CLI_SHOW_TOOL_INIT_LOGS"] = (
        "1" if bool(getattr(args, "show_mcp_logs", False)) else "0"
    )

    if args.list_sessions:
        await _list_sessions(workdir)
        return 0

    await _ensure_config_loaded(workdir)
    await _ensure_plugins_loaded(workdir)

    if args.list_agents:
        try:
            await _list_agents(workdir)
        finally:
            await MCPManager.close()
        return 0

    initial_prompt = str(args.prompt or "").strip()
    if not initial_prompt and not sys.stdin.isatty():
        initial_prompt = sys.stdin.read().strip()

    question_handler = question_handler or TerminalQuestionHandler(sys.stdin, sys.stderr)
    question_policy = normalize_question_policy(getattr(args, "question_policy", "ask"))
    agent, session, selector, provider = await _open_agent_session(
        workdir=workdir,
        session_id=str(args.session_id or "").strip(),
        requested_agent_name=str(args.agent or "").strip(),
        model_ref=str(args.model or "").strip(),
        title=str(args.title or "").strip(),
        execution_profile=args.execution_profile,
        reasoning_effort=args.reasoning_effort,
        scheduler_mode=args.scheduler_mode,
        resume_required=bool(args.resume),
        question_handler=question_handler,
        question_policy=question_policy,
    )

    state = {
        "agent": agent,
        "session": session,
        "selector": selector,
        "provider": provider,
        "model_ref": str(session.metadata.get("cli_model_ref") or "").strip(),
        "execution_profile": getattr(agent, "_execution_profile", None),
        "reasoning_effort": getattr(agent, "_reasoning_effort", None),
        "scheduler_mode": getattr(agent, "_scheduler_mode", None),
        "should_exit": False,
        "question_handler": question_handler,
        "question_policy": question_policy,
    }
    try:
        return await _interactive_loop(
            workdir=workdir,
            state=state,
            initial_prompt=initial_prompt,
            once=bool(args.once or getattr(args, "result_only", False)),
            final_reply=final_reply,
        )
    finally:
        # Also close resources on a failed turn, before emitting a clean result.
        await _close_provider(state["provider"])
        await MCPManager.close()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "runtimes":
        from .external_harness.cli import main as runtimes_main

        return runtimes_main(sys.argv[2:])
    parser = _build_parser()
    args = parser.parse_args()
    if args.result_only and (args.list_agents or args.list_sessions):
        parser.error("--result-only 不能与 --list-agents / --list-sessions 一起使用。")
    _bootstrap_path()

    try:
        with terminal_question_handler() as question_handler:
            if args.result_only:
                final_reply = _FinalReply()
                with _suppress_console_output():
                    exit_code = asyncio.run(_main_async(
                        args, final_reply=final_reply, question_handler=question_handler,
                    ))
                if final_reply.error:
                    print(final_reply.error, file=sys.stderr)
                elif exit_code == 0:
                    # Preserve the final Markdown text, without agent/log prefixes.
                    print(final_reply.content, end="" if final_reply.content.endswith("\n") else "\n")
                return exit_code
            return asyncio.run(_main_async(args, question_handler=question_handler))
    except KeyboardInterrupt:
        print("\n已中断运行。", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"tcode 启动失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
