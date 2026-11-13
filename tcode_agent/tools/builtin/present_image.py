"""Register a workspace image for structured frontend presentation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...presentation import inspect_image_file, make_presented_image_block
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext
from ..path_policy import resolve_workspace_io_path


class PresentImageParameters(ParameterSchema):
    path: str
    caption: str | None = ""
    alt: str | None = ""


class PresentImageTool(Tool):
    @property
    def id(self) -> str:
        return "present_image"

    @property
    def description(self) -> str:
        return """Display an existing image from the current workspace in the final answer.

Call this when a PNG, JPG, JPEG, WEBP, GIF, or BMP materially helps the user.
The tool validates and registers the image as a structured frontend block, so
you do not need to construct a Markdown image URL. Use a complete workspace-
relative path or `/workspace/...`; a bare filename is only valid at workspace root.
Do not call it for unrelated images or files outside the current workspace."""

    @property
    def parameters(self) -> type:
        return PresentImageParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = PresentImageParameters(**args)
        decision = resolve_workspace_io_path(
            parsed.path,
            context,
            tool_id=self.id,
            operation="read",
        )
        workdir_text = str((context.extra or {}).get("workdir") or "").strip()
        workdir = Path(
            workdir_text
            or str((decision.metadata or {}).get("workspace") or "")
            or Path.cwd()
        ).expanduser().resolve(strict=False)
        try:
            relative_path = decision.host_path.relative_to(workdir).as_posix()
        except Exception:
            relative_path = ""

        if decision.blocked or not relative_path:
            reason = decision.reason or (
                "Image presentation is limited to files inside the current workspace. "
                "Copy the image into the workspace first."
            )
            return ToolResult(
                title="Image not presented",
                output=reason,
                error=reason,
                metadata={**dict(decision.metadata or {}), "presentation_registered": False},
            )

        try:
            inspected = inspect_image_file(decision.host_path)
            block = make_presented_image_block(
                session_id=context.session_id,
                relative_path=relative_path,
                mime_type=str(inspected["mime_type"]),
                size=int(inspected["size"]),
                sha256=str(inspected["sha256"]),
                alt=parsed.alt or "",
                caption=parsed.caption or "",
            )
        except ValueError as exc:
            message = str(exc)
            return ToolResult(
                title="Image not presented",
                output=message,
                error=message,
                metadata={**dict(decision.metadata or {}), "presentation_registered": False},
            )

        return ToolResult(
            title="Image ready for display",
            output=(
                f"Registered image `{relative_path}` for frontend display "
                f"(asset_id={block['asset_id']}). It will be attached to the final answer."
            ),
            metadata={
                **dict(decision.metadata or {}),
                "presentation_registered": True,
                "content_block": block,
                "asset_id": block["asset_id"],
                "relative_path": relative_path,
                "mime_type": block["mime_type"],
                "size": block["size"],
            },
        )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
