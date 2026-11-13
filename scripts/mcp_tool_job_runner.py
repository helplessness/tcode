#!/usr/bin/env python3
"""Run one MCP tool call and persist raw result JSON.

This helper is designed to be launched by job_protocol_mcp.submit_job.
"""

import argparse
import asyncio
import json
import os
import time
import copy
from pathlib import Path

from tcode_agent.mcp.executor_config import ensure_executor_mcp_env
from tcode_agent.mcp.manager_auto import MCPManager


def _maybe_json_object(value):
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    return decoded
            except Exception:
                return value
    return value


def _normalize_args(payload: dict, tool_name: str) -> dict:
    """Normalize common wrapped/stringified argument formats.

    Examples handled:
    - {"input_config": "{\"text\":\"x\"}"} -> {"text":"x"}
    - {"input_config": {"text":"x"}} -> {"text":"x"}
    - {"input": "{...}"} -> {"input": {...}}
    """
    normalized = dict(payload)

    for key in ("input", "input_config", "payload"):
        if key in normalized:
            normalized[key] = _maybe_json_object(normalized[key])

    # Some MCP tools (e.g. *_mcp_server wrappers) expect a single input_config object.
    keep_input_config_wrapper = str(tool_name or "").endswith("_mcp_server")

    if set(normalized.keys()) == {"input_config"} and isinstance(normalized["input_config"], dict):
        if keep_input_config_wrapper:
            return {"input_config": normalized["input_config"]}
        return normalized["input_config"]

    # Enforce stable schema for *_mcp_server tools even if caller sends flat args.
    if keep_input_config_wrapper and "input_config" not in normalized:
        return {"input_config": normalized}

    return normalized


def _lock_path_for_output(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".lock")


def _extract_error_text(result: dict) -> str:
    content = result.get("content", []) if isinstance(result, dict) else []
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _business_result_state(value) -> tuple[bool | None, str]:
    """Return explicit business success/failure from nested MCP payloads."""
    if isinstance(value, list):
        saw_success = False
        for item in value:
            state, reason = _business_result_state(item)
            if state is False:
                return False, reason
            saw_success = saw_success or state is True
        return (True, "nested_success") if saw_success else (None, "")

    if not isinstance(value, dict):
        if isinstance(value, str):
            parsed = _maybe_json_object(value)
            if isinstance(parsed, dict):
                return _business_result_state(parsed)
        return None, ""

    if bool(value.get("isError")):
        return False, "mcp_is_error"
    for key in ("result_code", "success", "ok", "validated"):
        if key in value and value.get(key) is False:
            return False, f"{key}=false"
    for key in ("status", "state", "result_status"):
        status = str(value.get(key, "") or "").strip().lower()
        if status in {"failed", "error", "cancelled", "canceled", "terminated", "timed_out"}:
            return False, f"{key}={status}"

    saw_success = any(
        value.get(key) is True for key in ("result_code", "success", "ok", "validated")
    )
    if str(value.get("status", "") or "").strip().lower() in {"completed", "success", "done"}:
        saw_success = True

    for key in ("structuredContent", "result", "data", "summary", "content", "text"):
        if key not in value:
            continue
        state, reason = _business_result_state(value.get(key))
        if state is False:
            return False, reason
        saw_success = saw_success or state is True

    return (True, "explicit_success") if saw_success else (None, "")


def _is_gnina_autobox_type_error(server: str, tool: str, error_text: str) -> bool:
    if str(server).strip().lower() != "gnina-mcp" or str(tool).strip() != "gnina_mcp_server":
        return False
    text = str(error_text or "")
    return (
        "validation error for Gninaconfig".lower() in text.lower()
        and "autobox_ligand" in text
        and "Input should be a valid string" in text
    )


def _try_fix_gnina_autobox(payload: dict) -> tuple[dict, bool, str]:
    """Attempt to self-heal common gnina schema mismatch for autobox_ligand.

    - autobox_ligand=True  -> use ligand path string
    - autobox_ligand=False -> remove key
    """
    fixed = copy.deepcopy(payload)
    target = fixed
    if isinstance(fixed.get("input_config"), dict):
        target = fixed["input_config"]
    if not isinstance(target, dict):
        return payload, False, "payload_not_object"

    if "autobox_ligand" not in target:
        return payload, False, "autobox_ligand_missing"

    value = target.get("autobox_ligand")
    ligand = target.get("ligand")
    if value is True:
        ligand_text = str(ligand or "").strip()
        if ligand_text:
            target["autobox_ligand"] = ligand_text
            return fixed, True, "autobox_true_to_ligand_path"
        return payload, False, "autobox_true_but_ligand_missing"
    if value is False:
        target.pop("autobox_ligand", None)
        return fixed, True, "autobox_false_removed"

    return payload, False, "autobox_not_boolean"


def _output_is_success(output_path: Path) -> bool:
    if not output_path.is_file():
        return False
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        if data.get("isError"):
            return False
        business_state, _ = _business_result_state(data)
        if business_state is not None:
            return business_state
        return True
    except Exception:
        return False


def _try_acquire_lock(lock_path: Path) -> bool:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()} ts={time.time()}\n")
        return True
    except FileExistsError:
        return False


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def _acquire_or_wait_for_existing_result(output_path: Path) -> tuple[bool, str]:
    """Return (owned_lock, reason).

    - owned_lock=True: caller should execute MCP call and later release lock.
    - owned_lock=False and reason='reuse': existing runner already produced output.
    - owned_lock=False and reason='timeout': lock persisted too long without result.
    """
    lock_path = _lock_path_for_output(output_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    wait_seconds = int(os.environ.get("MCP_TOOL_RUNNER_DUP_WAIT_SECONDS", "900"))
    stale_seconds = int(os.environ.get("MCP_TOOL_RUNNER_LOCK_STALE_SECONDS", "1800"))
    deadline = time.time() + max(1, wait_seconds)

    while True:
        # Hard idempotency: once success exists, never re-execute expensive tool.
        if _output_is_success(output_path):
            return False, "reuse_success"

        if _try_acquire_lock(lock_path):
            # Re-check after lock acquisition in case another runner finished just now.
            if _output_is_success(output_path):
                _release_lock(lock_path)
                return False, "reuse_success"
            return True, "acquired"

        if output_path.is_file() and output_path.stat().st_size > 0:
            return False, "reuse"

        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > max(30, stale_seconds):
                _release_lock(lock_path)
                continue
        except FileNotFoundError:
            continue
        except Exception:
            pass

        if time.time() >= deadline:
            return False, "timeout"

        time.sleep(1)


async def _run(server: str, tool: str, args_obj: dict) -> dict:
    if not MCPManager.is_initialized():
        await MCPManager.initialize()

    async def _print_progress(payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        current = payload.get("progress")
        total = payload.get("total")
        message = str(payload.get("message", "") or "").strip()
        if not message:
            if current is not None and total is not None:
                message = f"MCP progress {current}/{total}"
            elif current is not None:
                message = f"MCP progress {current}"
            else:
                return
        event = {
            "message": message,
            "current": current,
            "total": total,
            "stream": "mcp",
            "status": "running",
        }
        print(
            f"::tcode-progress {json.dumps(event, ensure_ascii=False)}",
            flush=True,
        )

    return await MCPManager.call_tool(
        server,
        tool,
        args_obj,
        progress_callback=_print_progress,
        progress_token=f"runner:{os.getpid()}:{server}:{tool}",
    )


async def _run_with_retries(server: str, tool: str, payload: dict, max_retries: int) -> dict:
    attempt = 0
    current_payload = payload
    fix_applied = False
    fix_reason = ""
    result = None
    try:
        while True:
            attempt += 1
            result = await _run(server, tool, current_payload)
            if not (isinstance(result, dict) and result.get("isError")):
                break

            # Self-heal deterministic gnina schema mismatch before consuming retries.
            error_text = _extract_error_text(result)
            if not fix_applied and _is_gnina_autobox_type_error(server, tool, error_text):
                fixed_payload, changed, reason = _try_fix_gnina_autobox(current_payload)
                if changed:
                    current_payload = fixed_payload
                    fix_applied = True
                    fix_reason = reason
                    continue

            if attempt > max_retries:
                break
            time.sleep(1)

        if isinstance(result, dict):
            # Attach lightweight provenance for debugging duplicate-trigger incidents.
            result.setdefault("runner_meta", {})
            if not isinstance(result.get("runner_meta"), dict):
                result["runner_meta"] = {}
            result["runner_meta"].update(
                {
                    "attempts": attempt,
                    "max_retries": max_retries,
                    "auto_fix_applied": fix_applied,
                    "auto_fix_reason": fix_reason,
                }
            )
        return result or {}
    finally:
        try:
            await MCPManager.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one MCP tool call and save result.")
    parser.add_argument("--server", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--args-json", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=0)
    args = parser.parse_args()

    if args.timeout_seconds and args.timeout_seconds > 0:
        os.environ["TCODE_MCP_CALL_TIMEOUT_SECONDS"] = str(
            max(1, min(int(args.timeout_seconds), 24 * 60 * 60))
        )

    try:
        payload = json.loads(args.args_json)
        if not isinstance(payload, dict):
            raise ValueError("args-json must decode to an object")
        payload = _normalize_args(payload, args.tool)
    except Exception as exc:
        print(f"invalid args-json: {exc}")
        return 2

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    owned_lock, lock_reason = _acquire_or_wait_for_existing_result(output_path)
    if not owned_lock:
        if lock_reason in {"reuse", "reuse_success"}:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "output_file": str(output_path),
                        "deduped": True,
                        "reason": lock_reason,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print("duplicate runner wait timeout: output not ready")
        return 1

    lock_path = _lock_path_for_output(output_path)
    max_retries = int(os.environ.get("MCP_TOOL_RUNNER_MAX_RETRIES", "3"))
    max_retries = max(0, max_retries)
    try:
        resolved_config = ensure_executor_mcp_env(args.server)
        if resolved_config:
            print(
                "using executor MCP config for target server: "
                f"{args.server} -> {resolved_config}"
            )
        result = asyncio.run(_run_with_retries(args.server, args.tool, payload, max_retries))

        if isinstance(result, dict):
            # Attach lightweight provenance for debugging duplicate-trigger incidents.
            result.setdefault("runner_meta", {})
            if not isinstance(result.get("runner_meta"), dict):
                result["runner_meta"] = {}
            result["runner_meta"].update(
                {
                    "pid": os.getpid(),
                    "ts": time.time(),
                    "server": args.server,
                    "tool": args.tool,
                }
            )

        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"mcp call failed: {exc}")
        return 1
    finally:
        _release_lock(lock_path)

    # Mirror a concise message for job log tail.
    if isinstance(result, dict) and result.get("isError"):
        print(json.dumps({"ok": False, "error": result.get("content")}, ensure_ascii=False))
        return 1

    business_state, business_reason = _business_result_state(result)
    if business_state is False:
        print(
            json.dumps(
                {"ok": False, "error": business_reason or "business_result_failed"},
                ensure_ascii=False,
            )
        )
        return 1

    print(json.dumps({"ok": True, "output_file": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
