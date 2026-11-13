"""Inspect-file tool - bounded metadata and format preflight."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from ..base import ParameterSchema, Tool, ToolContext
from ..file_inspection import inspect_file_bounded
from ..path_policy import resolve_workspace_io_path
from ...types import ToolResult


class InspectFileParameters(ParameterSchema):
    filePath: str
    headBytes: int | None = None
    tailBytes: int | None = None
    previewChars: int | None = None


class InspectFileTool(Tool):
    """Inspect one file using a fixed-size head/tail sample."""

    @property
    def id(self) -> str:
        return "inspect_file"

    @property
    def description(self) -> str:
        return """Inspect a file before choosing how to read or parse it.

The tool obtains file size and reads only a bounded head/tail sample. It does
not scan the whole file, count every line, or compute a full-file hash. Use it
when a file's size, encoding, or format is unknown, especially before reading a
large attachment. The result recommends bounded Read, targeted search, or a
format-specific parser."""

    @property
    def parameters(self) -> type:
        return InspectFileParameters

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name, default))
        except Exception:
            value = default
        return max(minimum, min(value, maximum))

    @staticmethod
    def _bounded_int(value: int, *, minimum: int, maximum: int) -> int:
        return max(minimum, min(int(value), maximum))

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = InspectFileParameters(**args)
        decision = resolve_workspace_io_path(
            parsed.filePath,
            context,
            tool_id=self.id,
            operation="read",
        )
        if decision.blocked:
            return ToolResult(
                title=decision.display_path,
                output="",
                error=decision.reason,
                metadata=decision.metadata,
            )

        configured_head_bytes = self._env_int(
            "TCODE_FILE_PREFLIGHT_HEAD_BYTES",
            16 * 1024,
            minimum=512,
            maximum=256 * 1024,
        )
        head_bytes = self._bounded_int(
            parsed.headBytes if parsed.headBytes is not None else configured_head_bytes,
            minimum=512,
            maximum=configured_head_bytes,
        )
        configured_tail_bytes = self._env_int(
            "TCODE_FILE_PREFLIGHT_TAIL_BYTES",
            4 * 1024,
            minimum=0,
            maximum=64 * 1024,
        )
        tail_bytes = self._bounded_int(
            parsed.tailBytes if parsed.tailBytes is not None else configured_tail_bytes,
            minimum=0,
            maximum=configured_tail_bytes,
        )
        configured_preview_chars = self._env_int(
            "TCODE_FILE_PREFLIGHT_PREVIEW_CHARS",
            4 * 1024,
            minimum=256,
            maximum=32 * 1024,
        )
        preview_chars = self._bounded_int(
            parsed.previewChars if parsed.previewChars is not None else configured_preview_chars,
            minimum=256,
            maximum=configured_preview_chars,
        )
        large_file_bytes = self._env_int(
            "TCODE_FILE_PREFLIGHT_THRESHOLD_BYTES",
            5 * 1024 * 1024,
            minimum=1024,
            maximum=1024 * 1024 * 1024,
        )

        try:
            inspection = await asyncio.to_thread(
                inspect_file_bounded,
                decision.host_path,
                head_bytes=head_bytes,
                tail_bytes=tail_bytes,
                preview_chars=preview_chars,
                large_file_bytes=large_file_bytes,
            )
            payload = inspection.metadata(include_preview=True)
            payload["path"] = decision.display_path
            inspection_metadata = inspection.metadata(include_preview=False)
            inspection_metadata["path"] = decision.display_path
            return ToolResult(
                title=decision.display_path,
                output=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata={
                    "inspection": inspection_metadata,
                    "boundedInspection": True,
                    **decision.metadata,
                },
            )
        except FileNotFoundError:
            return ToolResult(
                title=decision.display_path,
                output="",
                error=f"File not found: {decision.display_path}",
                metadata=decision.metadata,
            )
        except Exception as exc:
            return ToolResult(
                title=decision.display_path,
                output="",
                error=str(exc),
                metadata=decision.metadata,
            )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


__all__ = ["InspectFileTool"]
