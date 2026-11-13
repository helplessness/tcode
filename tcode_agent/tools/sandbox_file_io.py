"""Small file operations executed inside the active sandbox in strict mode."""

from __future__ import annotations

import base64
import json
import os
import shlex
from typing import Any

from ..runtime.sandbox import build_tool_sandbox_spec, get_sandbox_runtime
from ..types import ToolResult
from .base import ToolContext
from .path_policy import WorkspacePathDecision


_WORKER = r'''
import base64
import glob
import json
import os
from pathlib import Path

request = json.loads(base64.b64decode(os.environ["TCODE_FILE_IO_REQUEST"]))
root = Path("/workspace")
root = Path(os.environ.get("TALL_WORKSPACE") or root).resolve(strict=True)
raw_path = request["path"]
path = (root / raw_path.removeprefix("/workspace").lstrip("/") if raw_path == "/workspace" or raw_path.startswith("/workspace/") else Path(raw_path)).resolve(strict=False)
try:
    path.relative_to(root)
except ValueError:
    raise SystemExit("file path escapes workspace")

operation = request["operation"]
try:
    if operation == "read":
        if path.stat().st_size > 128 * 1024:
            raise ValueError("strict read limit is 128 KiB; no data was read. For structured results use the discovered domain analysis tool to process the full file and return a bounded summary; do not retry read page-by-page or treat samples as full analysis.")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = max(0, int(request.get("offset", 1)) - 1)
        limit = max(1, min(2000, int(request.get("limit", 200))))
        output = "\n".join(f"{index + 1}: {line}" for index, line in enumerate(lines[start:start + limit], start))
        result = {"output": output, "line_count": len(lines),
                  "truncated": start + limit < len(lines), "prefixOmitted": start > 0,
                  "hasMore": start + limit < len(lines)}
    elif operation == "write":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request["content"], encoding="utf-8")
        result = {"output": "File written successfully"}
    elif operation == "edit":
        old = request["oldString"]
        if not old:
            raise ValueError("oldString must not be empty")
        content = path.read_text(encoding="utf-8")
        count = content.count(old)
        if count != 1:
            raise ValueError("oldString not found in file" if count == 0 else "oldString appears multiple times in file")
        path.write_text(content.replace(old, request["newString"], 1), encoding="utf-8")
        result = {"output": "File edited successfully"}
    elif operation == "glob":
        pattern = request["pattern"]
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("glob pattern escapes workspace")
        matches = []
        for candidate in sorted(glob.iglob(str(path / pattern), recursive=True)):
            resolved = Path(candidate).resolve(strict=False)
            if resolved.is_relative_to(root) and len(matches) < 1000:
                matches.append(resolved.as_posix())
        result = {"output": "\n".join(matches)[:128000], "count": len(matches)}
    elif operation == "grep":
        import fnmatch
        pattern = request["pattern"]
        include = request.get("include") or "*"
        matches, errors, scanned = [], [], 0
        if not path.exists():
            raise FileNotFoundError("search path does not exist")
        entries = [(str(path.parent), [], [path.name])] if path.is_file() else os.walk(path, onerror=lambda exc: errors.append(str(exc)))
        for parent, dirs, files in entries:
            dirs[:] = [d for d in dirs if d not in {".git", ".tcode", "node_modules", "__pycache__", ".venv"}]
            for name in files:
                if not fnmatch.fnmatch(name, include):
                    continue
                target = Path(parent, name).resolve(strict=False)
                if not target.is_relative_to(root):
                    continue
                try:
                    with target.open("r", encoding="utf-8", errors="ignore") as source:
                        scanned += 1
                        for number, line in enumerate(source, 1):
                            if pattern in line:
                                matches.append(f"{target}:{number}: {line.rstrip()}")
                                if len(matches) >= 1000:
                                    break
                except OSError as exc:
                    if len(errors) < 20:
                        errors.append(str(exc))
                if len(matches) >= 1000:
                    break
            if len(matches) >= 1000:
                break
        output = "\n".join(matches) or "No literal substring matches. Regex syntax is not interpreted; this does not establish absence of structured records."
        truncated = len(matches) >= 1000 or len(output) > 128000
        result = {"output": output[:128000], "count": len(matches), "scanned_files": scanned,
                  "complete": not errors and not truncated, "truncated": truncated}
        if errors:
            result["error"] = "search incomplete: " + "; ".join(errors)
    else:
        raise ValueError("unsupported file operation")
    print(json.dumps(result, ensure_ascii=False))
except (OSError, UnicodeError, ValueError) as exc:
    print(json.dumps({"error": str(exc)}, ensure_ascii=False))
'''.strip()


async def run_sandbox_file_io(
    *,
    operation: str,
    decision: WorkspacePathDecision,
    context: ToolContext,
    arguments: dict[str, Any],
    title: str | None = None,
) -> ToolResult:
    request = {"operation": operation, "path": decision.container_path, **arguments}
    encoded = base64.b64encode(json.dumps(request, ensure_ascii=False).encode()).decode()
    if len(encoded) > 60_000:
        return ToolResult(
            title=title or decision.display_path,
            output="",
            error="strict file operation exceeds 60 KiB; use the sandbox shell for large writes",
            metadata=decision.metadata,
        )
    try:
        native = os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
        if native and operation in {"write", "edit"} and decision.relative_path.split('/')[0] == 'upload':
            raise ValueError('导入输入只读，请把修改后的文件写入 artifacts 目录')
        spec = build_tool_sandbox_spec(
            tool_name=operation,
            command=f"python3 -I -c {shlex.quote(_WORKER)}",
            requested_workdir=None,
            timeout_seconds=30,
            context=context,
            extra_env={"TCODE_FILE_IO_REQUEST": encoded},
        )
        if not native and (spec.network != "none" or spec.user in {"0", "root"} or spec.user.startswith("0:") or spec.read_only is not True):
            raise RuntimeError("strict file operations require a non-root, read-only, offline profile")
        result = await get_sandbox_runtime().run_once(spec)
        if result.exit_code != 0 or result.status != "completed":
            raise RuntimeError(result.stderr or result.stdout or result.status)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("invalid sandbox file response")
        if native:
            from ..runtime.sandbox.macos_native import _jobs, publish_file
            state = _jobs[result.sandbox_job_id]
            payload['output'] = str(payload.get('output') or '').replace(str(state['work']), '/workspace')
            if operation in {'write', 'edit'} and not payload.get('error'):
                relative = decision.relative_path
                publish_file(state['workspace'], relative, (state['work'] / relative).read_bytes(), expected=state['hashes'].get(relative))
        return ToolResult(
            title=title or decision.display_path,
            output=str(payload.get("output") or ""),
            error=str(payload.get("error") or "") or None,
            metadata={**decision.metadata, "sandbox": True,
                      **{key: payload[key] for key in ('count', 'scanned_files', 'complete', 'truncated', 'prefixOmitted', 'hasMore') if key in payload}},
        )
    except Exception as exc:
        return ToolResult(
            title=title or decision.display_path,
            output="",
            error=f"sandbox file operation failed: {exc}",
            metadata=decision.metadata,
        )
