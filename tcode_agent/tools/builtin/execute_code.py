"""Restricted async Python glue tool for low-risk multi-step analysis."""

from __future__ import annotations

import asyncio
import collections
import datetime as datetime_module
import io
import json
import math
import re
import statistics
import textwrap
from typing import Any

from ..base import ParameterSchema, Tool, ToolContext
from ..registry import ToolRegistry
from ...types import ToolResult


_SAFE_TOOL_IDS = (
    "read",
    "glob",
    "grep",
    "webfetch",
    "websearch",
    "session_search",
    "memory_read",
)
_MAX_TOOL_CALLS = 24
_DEFAULT_TIMEOUT_SECONDS = 20
_MAX_TIMEOUT_SECONDS = 60
_MAX_STDOUT_CHARS = 12000

_SAFE_BUILTINS = {
    "len": len,
    "range": range,
    "min": min,
    "max": max,
    "sum": sum,
    "sorted": sorted,
    "enumerate": enumerate,
    "zip": zip,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "any": any,
    "all": all,
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "RuntimeError": RuntimeError,
    "isinstance": isinstance,
}


class ExecuteCodeParameters(ParameterSchema):
    code: str
    timeout_seconds: int | None = _DEFAULT_TIMEOUT_SECONDS


class ExecuteCodeTool(Tool):
    @property
    def id(self) -> str:
        return "execute_code"

    @property
    def description(self) -> str:
        return """Run restricted async Python for small, read-only glue tasks.

This is NOT the workspace package/runtime sandbox. For third-party packages,
dependency installation, arbitrary Python modules, long-running computation, or
background processes, use `exec` and manage it with `process`.

Use this when you need multiple safe read-only tool calls plus local processing in one turn,
for example:
- 聚合多个 `read` / `grep` / `glob` 结果
- 对检索结果做筛选、去重、统计
- 将多段信息整理成表格或 JSON

Important constraints:
- Your code runs inside `async def`, so call helpers with `await`
- Available helpers: `read`, `glob`, `grep`, `webfetch`, `websearch`, `session_search`, `memory_read`
- Long documents: if webfetch reports a preview, use `await webfetch(document_id=..., view="evidence", query="keyword")` or view="sections". For sequential reading use next_offset with the same view and filters.
- Available modules without importing: `json`, `re`, `math`, `statistics`, `collections`, `datetime`
- Imports and `asyncio` are unavailable; no file writes, bash, `task`, or domain MCP submit
- Prefer this for glue logic, not for high-cost execution or workflow submission
"""

    @property
    def parameters(self) -> type:
        return ExecuteCodeParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ExecuteCodeParameters(**args)
        code = str(parsed.code or "").rstrip()
        if not code:
            return ToolResult(title="execute_code", output="", error="code is required")

        timeout_seconds = int(parsed.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS)
        timeout_seconds = max(1, min(timeout_seconds, _MAX_TIMEOUT_SECONDS))

        stdout_buffer = io.StringIO()
        tool_call_state = {"count": 0}
        web_previews = {}

        def with_web_previews(output: str) -> str:
            # Keep continuation visible even when the script only prints a count
            # or a later helper raises. Do not rely on the script echoing metadata.
            notice = "\n".join(f"[webfetch: {item}]" for item in web_previews.values())
            return f"{notice}\n\n{output}" if notice else output

        def _print(*values, sep: str = " ", end: str = "\n"):
            stdout_buffer.write(sep.join(str(v) for v in values) + end)

        async def _call_tool(tool_id: str, tool_args: dict[str, Any]) -> dict[str, Any]:
            if tool_call_state["count"] >= _MAX_TOOL_CALLS:
                raise RuntimeError(
                    f"execute_code exceeded max tool calls ({_MAX_TOOL_CALLS})"
                )
            tool = ToolRegistry.get(tool_id)
            if tool is None:
                raise RuntimeError(f"Tool not available in execute_code: {tool_id}")
            tool_call_state["count"] += 1
            result = await tool.execute(tool_args, context)
            if result.error:
                raise RuntimeError(f"{tool_id} failed: {result.error}")
            if tool_id == "webfetch" and (notice := result.metadata.get("continuation_notice")):
                web_previews[result.metadata.get("document_id") or tool_args.get("url")] = notice
            return {
                "title": result.title,
                "output": result.output,
                "metadata": dict(result.metadata or {}),
                "output_path": result.output_path,
            }

        async def read(file_path: str, offset: int = 1, limit: int | None = None):
            return await _call_tool(
                "read",
                {"filePath": file_path, "offset": offset, "limit": limit},
            )

        async def glob(pattern: str, path: str | None = None):
            return await _call_tool("glob", {"pattern": pattern, "path": path})

        async def grep(pattern: str, path: str | None = None, include: str | None = None):
            return await _call_tool(
                "grep",
                {"pattern": pattern, "path": path, "include": include},
            )

        async def webfetch(url: str = "", format: str | None = "markdown", *, view=None, section="", query="", offset=0, document_id=""):
            return await _call_tool("webfetch", {"url": url, "format": format, "view": view,
                                                 "section": section, "query": query, "offset": offset,
                                                 "document_id": document_id})

        async def websearch(query: str, count: int | None = 5):
            return await _call_tool("websearch", {"query": query, "count": count})

        async def session_search(
            query: str | None = None,
            limit: int | None = 5,
            session_id: str | None = None,
            revision: int | None = None,
            include_history: bool | None = False,
            list_revisions: bool | None = False,
        ):
            return await _call_tool(
                "session_search",
                {
                    "query": query,
                    "limit": limit,
                    "session_id": session_id,
                    "revision": revision,
                    "include_history": include_history,
                    "list_revisions": list_revisions,
                },
            )

        async def memory_read(
            query: str | None = None,
            limit: int | None = 10,
            level: str | None = "l1",
            scope: str | None = "auto",
        ):
            return await _call_tool(
                "memory_read",
                {"query": query, "limit": limit, "level": level, "scope": scope},
            )

        globals_env: dict[str, Any] = {
            "__builtins__": dict(_SAFE_BUILTINS, print=_print),
            "json": json,
            "re": re,
            "math": math,
            "statistics": statistics,
            "collections": collections,
            "datetime": datetime_module,
            "read": read,
            "glob": glob,
            "grep": grep,
            "webfetch": webfetch,
            "websearch": websearch,
            "session_search": session_search,
            "memory_read": memory_read,
        }
        locals_env: dict[str, Any] = dict(globals_env)

        source = "async def __user_code__():\n" + textwrap.indent(code, "    ")
        try:
            exec(compile(source, "<execute_code>", "exec"), globals_env, locals_env)
            user_fn = locals_env["__user_code__"]
            returned = await asyncio.wait_for(user_fn(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return ToolResult(
                title="execute_code",
                output=with_web_previews(stdout_buffer.getvalue())[:_MAX_STDOUT_CHARS],
                error=f"execute_code timed out after {timeout_seconds}s",
            )
        except Exception as exc:
            error = str(exc)
            if isinstance(exc, NameError):
                missing = str(getattr(exc, "name", "") or "").strip()
                prefix = f"`{missing}` is unavailable" if missing else "A name is unavailable"
                error = (
                    f"{prefix} in restricted execute_code. Imports and asyncio are not "
                    "supported here. Use only the documented helpers/modules, or use "
                    "`exec` + `process` for packages, arbitrary Python, or long-running work."
                )
            return ToolResult(
                title="execute_code",
                output=with_web_previews(stdout_buffer.getvalue())[:_MAX_STDOUT_CHARS],
                error=error,
            )

        output = stdout_buffer.getvalue().strip()
        if not output and returned is not None:
            if isinstance(returned, (dict, list, tuple)):
                output = json.dumps(returned, ensure_ascii=False, indent=2)
            else:
                output = str(returned)
        output = with_web_previews(output)
        if len(output) > _MAX_STDOUT_CHARS:
            output = output[:_MAX_STDOUT_CHARS] + "\n...[truncated]..."

        return ToolResult(
            title="execute_code",
            output=output,
            metadata={
                "tool_calls": tool_call_state["count"],
                "allowed_tools": list(_SAFE_TOOL_IDS),
                "timeout_seconds": timeout_seconds,
            },
        )

    @classmethod
    def register(cls):
        ToolRegistry.register(cls())
