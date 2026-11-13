"""Read tool - bounded, paginated text-file reads."""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
from typing import Any, TextIO

from ..base import ParameterSchema, Tool, ToolContext
from ..file_inspection import (
    FileInspection,
    inspect_file_bounded,
    open_regular_file_no_symlinks,
)
from ..path_policy import resolve_workspace_io_path
from ...types import ToolResult


class ReadParameters(ParameterSchema):
    """Parameters for read tool."""

    filePath: str
    limit: int | None = None
    offset: int | None = 1
    columnOffset: int | None = 0


class ReadTool(Tool):
    """Read a bounded window from a text file."""

    DEFAULT_LIMIT_LINES = 2000
    MAX_OUTPUT_CHARS = 80000
    LARGE_FILE_BYTES = 5 * 1024 * 1024
    MAX_LIMIT_LINES = 50000
    MAX_CONFIGURED_OUTPUT_CHARS = 2_000_000
    DISCARD_CHUNK_CHARS = 64 * 1024
    SMALL_PAGE_LIMIT_LINES = 5
    MAX_CONSECUTIVE_SMALL_PAGES = 8

    def __init__(self) -> None:
        self._pagination_state: dict[tuple[str, str], dict[str, Any]] = {}

    @property
    def id(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return """Read a bounded, line-numbered window from a text file.

IMPORTANT:
- For large structured scoring/results tables, use a discovered domain analysis tool to process all rows and return statistics/artifact references. Do not page the entire dataset into model context. Use read only for small samples or debugging; samples do not prove full coverage.
- Use relative paths (for example `src/main.py`) from the current working directory
- Use `offset` and `limit` to page through large files
- Always follow both returned cursor fields: `nextOffset` and `nextColumnOffset`
- Reads stop as soon as the line or character budget is reached and return a truncation marker
- Do not use `workdir/` as a path prefix
- In sandbox mode, paths must stay inside the active workspace; `/workspace/...` is supported

Examples:
- `{"filePath": "src/main.py"}` reads the first bounded window
- `{"filePath": "src/main.py", "offset": 2001, "limit": 500}` continues at line 2001
- `{"filePath": "data.json", "offset": 1, "columnOffset": 80000}` continues an oversized line
- `{"filePath": "/workspace/input.pdb", "limit": 300}` reads a sandbox-visible file"""

    @property
    def parameters(self) -> type:
        return ReadParameters

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name, default))
        except Exception:
            value = default
        return max(minimum, min(value, maximum))

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _discard_line_remainder(cls, stream: TextIO) -> bool:
        """Discard one oversized logical line with bounded memory.

        Returns True when EOF was reached while discarding.
        """
        while True:
            chunk = stream.readline(cls.DISCARD_CHUNK_CHARS)
            if chunk == "":
                return True
            if chunk.endswith("\n"):
                return False

    @classmethod
    def _skip_line_columns(cls, stream: TextIO, columns: int) -> None:
        """Position within one logical line without loading it all into memory."""
        remaining = max(0, int(columns))
        while remaining > 0:
            chunk = stream.readline(min(remaining, cls.DISCARD_CHUNK_CHARS))
            if chunk == "":
                raise ValueError("columnOffset exceeds the target line length")
            if chunk.endswith("\n"):
                raise ValueError("columnOffset exceeds the target line length")
            remaining -= len(chunk)

    @staticmethod
    def _append_rendered_line(
        output_lines: list[str],
        rendered: str,
        output_chars: int,
    ) -> int:
        if output_lines:
            output_chars += 1
        output_lines.append(rendered)
        return output_chars + len(rendered)

    @classmethod
    def _read_stream(
        cls,
        stream: TextIO,
        *,
        offset: int,
        column_offset: int = 0,
        limit: int,
        max_output_chars: int,
    ) -> dict[str, Any]:
        output_lines: list[str] = []
        output_chars = 0
        line_number = 0
        eof_reached = False
        line_limit_truncated = False
        char_truncated = False
        partial_line_truncated = False
        next_offset: int | None = None
        next_column_offset: int | None = None
        target_line_positioned = False

        while True:
            next_line_number = line_number + 1

            # Skip preceding lines in bounded chunks so a minified/oversized
            # line cannot be loaded into memory all at once.
            if next_line_number < offset:
                chunk = stream.readline(cls.DISCARD_CHUNK_CHARS)
                if chunk == "":
                    eof_reached = True
                    break
                line_number = next_line_number
                if not chunk.endswith("\n"):
                    eof_reached = cls._discard_line_remainder(stream)
                    if eof_reached:
                        break
                continue

            active_column_offset = column_offset if next_line_number == offset else 0
            if not target_line_positioned and next_line_number == offset:
                if active_column_offset:
                    cls._skip_line_columns(stream, active_column_offset)
                target_line_positioned = True

            # One-character lookahead is sufficient to tell whether another
            # page exists. Do not scan the remainder of the file.
            if len(output_lines) >= limit:
                if stream.read(1) == "":
                    eof_reached = True
                else:
                    line_limit_truncated = True
                    next_offset = next_line_number
                    next_column_offset = 0
                break

            separator_chars = 1 if output_lines else 0
            prefix = (
                f"{next_line_number}@{active_column_offset}: "
                if active_column_offset
                else f"{next_line_number}: "
            )
            remaining = (
                max_output_chars
                - output_chars
                - separator_chars
                - len(prefix)
            )
            if remaining <= 0:
                if stream.read(1) == "":
                    eof_reached = True
                else:
                    char_truncated = True
                    next_offset = next_line_number
                    next_column_offset = active_column_offset
                break

            # Never read beyond the remaining budget. If this ends in the
            # middle of a logical line, the next cursor resumes that same line.
            chunk = stream.readline(remaining)
            if chunk == "":
                eof_reached = True
                break
            line_number = next_line_number

            if chunk.endswith("\n"):
                content = chunk[:-1]
                if content.endswith("\r"):
                    content = content[:-1]
                output_chars = cls._append_rendered_line(
                    output_lines,
                    prefix + content,
                    output_chars,
                )
                continue

            # A short non-newline read means EOF. A full-budget read needs a
            # non-consuming lookahead to distinguish exact EOF/newline from a
            # continuation of the same logical line.
            if len(chunk) < remaining:
                output_chars = cls._append_rendered_line(
                    output_lines,
                    prefix + chunk,
                    output_chars,
                )
                eof_reached = True
                break

            position = stream.tell()
            lookahead = stream.read(1)
            if lookahead == "":
                output_chars = cls._append_rendered_line(
                    output_lines,
                    prefix + chunk,
                    output_chars,
                )
                eof_reached = True
                break
            if lookahead == "\n":
                output_chars = cls._append_rendered_line(
                    output_lines,
                    prefix + chunk,
                    output_chars,
                )
                continue
            stream.seek(position)

            output_chars = cls._append_rendered_line(
                output_lines,
                prefix + chunk,
                output_chars,
            )
            char_truncated = True
            partial_line_truncated = True
            next_offset = line_number
            next_column_offset = active_column_offset + len(chunk)
            break

        has_more = next_offset is not None
        prefix_omitted = offset > 1 or column_offset > 0
        # `truncated` means more data remains after this cursor. Starting from
        # a non-zero cursor is intentional pagination, not a failed/truncated
        # page by itself.
        truncated = has_more
        output = "\n".join(output_lines)

        if has_more:
            if partial_line_truncated:
                marker = (
                    f"[read truncated at {max_output_chars} characters while reading "
                    f"line {line_number}; no content was discarded. Continue with "
                    f"offset={next_offset}, columnOffset={next_column_offset}, "
                    f"limit={limit}.]"
                )
            else:
                marker = (
                    f"[read truncated; continue with offset={next_offset}, "
                    f"columnOffset={next_column_offset or 0}, limit={limit}.]"
                )
            output = f"{output}\n{marker}" if output else marker

        return {
            "output": output,
            "content_output_chars": output_chars,
            "output_chars": len(output),
            "lines_scanned": line_number,
            "total_lines": line_number if eof_reached else None,
            "total_lines_known": eof_reached,
            "truncated": truncated,
            "prefix_omitted": prefix_omitted,
            "has_more": has_more,
            "next_offset": next_offset,
            "next_column_offset": next_column_offset,
            "line_limit_truncated": line_limit_truncated,
            "char_truncated": char_truncated,
            "partial_line_truncated": partial_line_truncated,
        }

    @classmethod
    def _read_file(
        cls,
        filepath: Path,
        *,
        offset: int,
        column_offset: int,
        limit: int,
        max_output_chars: int,
    ) -> tuple[dict[str, Any], int, tuple[int, int, int]]:
        file_fd, info = open_regular_file_no_symlinks(filepath)
        try:
            file_size = int(info.st_size)
            prefix = os.read(file_fd, 3)
            os.lseek(file_fd, 0, os.SEEK_SET)
            if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
                encoding = "utf-16"
            elif prefix.startswith(b"\xef\xbb\xbf"):
                encoding = "utf-8-sig"
            else:
                encoding = "utf-8"
            with io.TextIOWrapper(
                os.fdopen(file_fd, "rb", closefd=False),
                encoding=encoding,
                errors="strict",
                newline=None,
            ) as stream:
                window = cls._read_stream(
                    stream,
                    offset=offset,
                    column_offset=column_offset,
                    limit=limit,
                    max_output_chars=max_output_chars,
                )
            final_info = os.fstat(file_fd)
            final_fingerprint = (
                int(final_info.st_ino),
                int(final_info.st_size),
                int(final_info.st_mtime_ns),
            )
            initial_fingerprint = (
                int(info.st_ino),
                file_size,
                int(info.st_mtime_ns),
            )
            if final_fingerprint != initial_fingerprint:
                raise RuntimeError("file changed while it was being read; retry")
            return window, file_size, initial_fingerprint
        finally:
            os.close(file_fd)

    @classmethod
    def _inspect_file(cls, filepath: Path, *, large_file_bytes: int) -> FileInspection:
        return inspect_file_bounded(
            filepath,
            head_bytes=cls._env_int(
                "TCODE_FILE_PREFLIGHT_HEAD_BYTES",
                16 * 1024,
                minimum=512,
                maximum=256 * 1024,
            ),
            tail_bytes=cls._env_int(
                "TCODE_FILE_PREFLIGHT_TAIL_BYTES",
                4 * 1024,
                minimum=0,
                maximum=64 * 1024,
            ),
            preview_chars=cls._env_int(
                "TCODE_FILE_PREFLIGHT_PREVIEW_CHARS",
                4 * 1024,
                minimum=256,
                maximum=32 * 1024,
            ),
            large_file_bytes=large_file_bytes,
        )

    def _pagination_guard_result(
        self,
        *,
        decision_metadata: dict[str, Any],
        display_path: str,
        file_size: int,
        limit: int,
        offset: int,
        column_offset: int,
        count: int,
    ) -> ToolResult:
        reason = (
            "Detected repeated tiny paginated reads of the same file. This can "
            "exhaust the per-turn tool budget before the task makes progress. "
            "Use a larger read limit, a targeted search, or a parser/summarizer "
            "tool instead of continuing one line at a time."
        )
        return ToolResult(
            title=display_path,
            output="",
            error=reason,
            metadata={
                "read_pagination_guard": True,
                "retryable": False,
                "terminal_tool_error": True,
                "should_end_turn": True,
                "fileSizeBytes": file_size,
                "limit": limit,
                "offset": offset,
                "columnOffset": column_offset,
                "consecutiveSmallPageReads": count,
                **decision_metadata,
            },
        )

    def _update_pagination_guard(
        self,
        *,
        context: ToolContext,
        filepath: Path,
        requested_limit: int | None,
        effective_limit: int,
        offset: int,
        column_offset: int,
        window: dict[str, Any],
    ) -> int:
        small_page_limit = self._env_int(
            "TCODE_READ_SMALL_PAGE_LIMIT_LINES",
            self.SMALL_PAGE_LIMIT_LINES,
            minimum=1,
            maximum=1000,
        )
        max_small_pages = self._env_int(
            "TCODE_READ_MAX_CONSECUTIVE_SMALL_PAGES",
            self.MAX_CONSECUTIVE_SMALL_PAGES,
            minimum=2,
            maximum=1000,
        )
        key = (str(context.session_id or ""), str(filepath))
        is_small_explicit_page = (
            requested_limit is not None
            and effective_limit <= small_page_limit
            and bool(window.get("has_more"))
        )
        if not is_small_explicit_page:
            self._pagination_state.pop(key, None)
            return 0

        previous = self._pagination_state.get(key) or {}
        follows_previous_cursor = (
            previous.get("next_offset") == offset
            and previous.get("next_column_offset", 0) == column_offset
        )
        count = int(previous.get("count", 0) or 0) + 1 if follows_previous_cursor else 1
        self._pagination_state[key] = {
            "count": count,
            "next_offset": window.get("next_offset"),
            "next_column_offset": window.get("next_column_offset") or 0,
        }
        return count if count > max_small_pages else 0

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = ReadParameters(**args)
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
        from ...runtime.sandbox.base import file_isolation_enabled

        if file_isolation_enabled():
            from ..sandbox_file_io import run_sandbox_file_io

            return await run_sandbox_file_io(
                operation="read",
                decision=decision,
                context=context,
                arguments={"offset": parsed.offset or 1, "limit": parsed.limit or self.DEFAULT_LIMIT_LINES},
            )
        filepath = decision.host_path

        default_limit = self._env_int(
            "TCODE_READ_DEFAULT_LIMIT_LINES",
            self.DEFAULT_LIMIT_LINES,
            minimum=1,
            maximum=self.MAX_LIMIT_LINES,
        )
        max_output_chars = self._env_int(
            "TCODE_READ_MAX_OUTPUT_CHARS",
            self.MAX_OUTPUT_CHARS,
            minimum=1000,
            maximum=self.MAX_CONFIGURED_OUTPUT_CHARS,
        )
        large_file_bytes = self._env_int(
            "TCODE_READ_LARGE_FILE_BYTES",
            self.LARGE_FILE_BYTES,
            minimum=1024,
            maximum=1024 * 1024 * 1024,
        )
        preflight_large_file_bytes = self._env_int(
            "TCODE_FILE_PREFLIGHT_THRESHOLD_BYTES",
            large_file_bytes,
            minimum=1024,
            maximum=1024 * 1024 * 1024,
        )
        offset = max(1, int(parsed.offset or 1))
        column_offset = max(0, int(parsed.columnOffset or 0))
        requested_limit = parsed.limit
        effective_limit = (
            max(1, min(int(requested_limit), self.MAX_LIMIT_LINES))
            if requested_limit
            else default_limit
        )
        inspection: FileInspection | None = None
        preflight_applied = False

        def inspection_metadata() -> dict[str, Any] | None:
            if inspection is None:
                return None
            payload = inspection.metadata(include_preview=False)
            payload["path"] = decision.display_path
            return payload

        # The ordinary first read is the high-risk path: the model knows only a
        # filename and may not know whether it is a database, archive, or a
        # minified multi-megabyte text file. Explicit cursor reads remain
        # bounded by the existing character budget and do not repeat preflight.
        initial_default_read = (
            requested_limit in (None, 0)
            and offset == 1
            and column_offset == 0
        )
        if initial_default_read and self._env_bool("TCODE_FILE_PREFLIGHT_ENABLED", True):
            try:
                inspection = await asyncio.to_thread(
                    self._inspect_file,
                    filepath,
                    large_file_bytes=preflight_large_file_bytes,
                )
                preflight_applied = True
            except FileNotFoundError:
                inspection = None
            except Exception as exc:
                return ToolResult(
                    title=decision.display_path,
                    output="",
                    error=f"File preflight failed: {exc}",
                    metadata={
                        "filePreflightFailed": True,
                        "retryable": True,
                        **decision.metadata,
                    },
                )

        if inspection is not None and not inspection.is_text:
            return ToolResult(
                title=decision.display_path,
                output="",
                error=(
                    "The file is binary or uses an unsupported text encoding. "
                    "Use inspect_file and "
                    f"the recommended parser ({inspection.recommended_tool}) instead of Read."
                ),
                metadata={
                    "preflightApplied": True,
                    "inspection": inspection_metadata(),
                    "fileSizeBytes": inspection.size_bytes,
                    "largeFile": inspection.large_file,
                    "retryable": False,
                    **decision.metadata,
                },
            )

        if inspection is not None and inspection.large_file:
            effective_limit = min(
                effective_limit,
                self._env_int(
                    "TCODE_READ_LARGE_INITIAL_LINES",
                    100,
                    minimum=1,
                    maximum=self.MAX_LIMIT_LINES,
                ),
            )
            max_output_chars = min(
                max_output_chars,
                self._env_int(
                    "TCODE_READ_LARGE_INITIAL_CHARS",
                    12_000,
                    minimum=1000,
                    maximum=self.MAX_CONFIGURED_OUTPUT_CHARS,
                ),
            )

        try:
            window, file_size, file_fingerprint = await asyncio.to_thread(
                self._read_file,
                filepath,
                offset=offset,
                column_offset=column_offset,
                limit=effective_limit,
                max_output_chars=max_output_chars,
            )
            if inspection is not None and file_fingerprint != inspection.fingerprint:
                return ToolResult(
                    title=decision.display_path,
                    output="",
                    error="File changed during preflight; retry the read against the current file.",
                    metadata={
                        "preflightApplied": True,
                        "fileChangedDuringRead": True,
                        "retryable": True,
                        **decision.metadata,
                    },
                )
            guard_count = self._update_pagination_guard(
                context=context,
                filepath=filepath,
                requested_limit=requested_limit,
                effective_limit=effective_limit,
                offset=offset,
                column_offset=column_offset,
                window=window,
            )
            if guard_count:
                return self._pagination_guard_result(
                    decision_metadata=decision.metadata,
                    display_path=decision.display_path,
                    file_size=file_size,
                    limit=effective_limit,
                    offset=offset,
                    column_offset=column_offset,
                    count=guard_count,
                )
            return ToolResult(
                title=decision.display_path,
                output=window["output"],
                truncated=bool(window["truncated"]),
                metadata={
                    "totalLines": window["total_lines"],
                    "totalLinesKnown": window["total_lines_known"],
                    "linesScanned": window["lines_scanned"],
                    "truncated": window["truncated"],
                    "prefixOmitted": window["prefix_omitted"],
                    "hasMore": window["has_more"],
                    "nextOffset": window["next_offset"],
                    "nextColumnOffset": window["next_column_offset"],
                    "offset": offset,
                    "columnOffset": column_offset,
                    "limit": effective_limit,
                    "requestedLimit": requested_limit,
                    "defaultLimitApplied": requested_limit in (None, 0),
                    "maxOutputChars": max_output_chars,
                    "contentOutputChars": window["content_output_chars"],
                    "outputChars": window["output_chars"],
                    "lineLimitTruncated": window["line_limit_truncated"],
                    "charTruncated": window["char_truncated"],
                    "partialLineTruncated": window["partial_line_truncated"],
                    "fileSizeBytes": file_size,
                    "largeFile": file_size >= large_file_bytes,
                    "largeFileThresholdBytes": large_file_bytes,
                    "preflightApplied": preflight_applied,
                    "inspection": (
                        inspection_metadata()
                    ),
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
