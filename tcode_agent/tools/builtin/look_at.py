"""Inspect a workspace image through a configured visual-model bridge."""

from __future__ import annotations

import base64
import inspect
from pathlib import Path
from typing import Any

from ...config.manager import ConfigManager
from ...presentation import inspect_image_file
from ...providers import create_provider
from ...providers.base import ChatMessage
from ...types import ToolResult
from ...vision import select_vision_model_ref
from ..base import ParameterSchema, Tool, ToolContext
from ..path_policy import resolve_workspace_io_path


class LookAtParameters(ParameterSchema):
    path: str
    question: str | None = ""


class LookAtTool(Tool):
    @property
    def id(self) -> str:
        return "look_at"

    @property
    def description(self) -> str:
        return """Inspect an image in the current workspace with a visual model.

Use this when the user asks about an uploaded image but your current model cannot
directly see image content, or when visual details must be verified. Pass the
complete workspace-relative path or `/workspace/...` path shown in the user
message. The tool returns visual observations as text for you to reason over.
If it reports that visual inspection is unavailable, explain that limitation to
the user in normal language instead of exposing a provider/API error."""

    @property
    def parameters(self) -> type:
        return LookAtParameters

    @staticmethod
    async def _close_provider(provider: Any) -> None:
        close_fn = getattr(provider, "close", None)
        if not callable(close_fn):
            return
        try:
            result = close_fn()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    @staticmethod
    def _unavailable(reason: str, *, metadata: dict[str, Any]) -> ToolResult:
        return ToolResult(
            title="Visual inspection unavailable",
            output=(
                "视觉检查暂时不可用。"
                f"{reason}"
                "请根据这一诊断用自然语言告知用户；不要声称已经看到了图片，"
                "也不要展示底层供应商或 API 报错。"
            ),
            metadata={**metadata, "vision_status": "unavailable"},
        )

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = LookAtParameters(**args)
        decision = resolve_workspace_io_path(
            parsed.path,
            context,
            tool_id=self.id,
            operation="read",
        )
        metadata = dict(decision.metadata or {})
        if decision.blocked:
            return self._unavailable(
                "图片路径不在当前会话工作区内或不可访问。",
                metadata=metadata,
            )

        try:
            inspected = inspect_image_file(decision.host_path)
        except (OSError, ValueError, TypeError):
            return self._unavailable(
                "指定文件不是可读取的 PNG、JPEG、WebP、GIF 或 BMP 图片。",
                metadata=metadata,
            )

        maximum = 10 * 1024 * 1024
        if int(inspected.get("size") or 0) > maximum:
            return self._unavailable(
                "图片超过视觉检查的 10 MB 大小限制。",
                metadata=metadata,
            )

        workdir = str((context.extra or {}).get("workdir") or Path.cwd()).strip()
        ConfigManager.load(workdir)
        config = ConfigManager.get_config_copy()
        current_provider = (context.extra or {}).get("provider")
        current_model = str(
            getattr(current_provider, "_model", "")
            or getattr(current_provider, "model", "")
            or ""
        ).strip()
        vision_model_ref = select_vision_model_ref(config)
        if not vision_model_ref:
            return self._unavailable(
                "当前工作区没有配置可供 Agent 调用的视觉模型。",
                metadata=metadata,
            )

        image_path = Path(str(inspected["path"]))
        mime_type = str(inspected.get("mime_type") or "image/jpeg")
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_path.read_bytes()).decode('ascii')}"
        question = str(parsed.question or "").strip() or (
            "请客观描述图片中的关键内容，并提取回答用户问题所需的文字、"
            "表格、图形关系和不确定之处。"
        )
        provider = None
        try:
            provider = create_provider(workdir=workdir, model_ref=vision_model_ref)
            response = await provider.chat(
                messages=[
                    ChatMessage(
                        role="system",
                        content=(
                            "你是 Agent 的视觉检查子系统。只报告图片中可观察到的内容；"
                            "不要猜测看不清的信息。保留图表、表格和文字之间的关系。"
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=question,
                        images=[
                            {
                                "data_url": data_url,
                                "mime_type": mime_type,
                                "detail": "high",
                                "source": "agent_vision_tool",
                            }
                        ],
                    ),
                ],
                max_tokens=1200,
            )
            observation = str(getattr(response, "content", "") or "").strip()
            if not observation:
                return self._unavailable(
                    "视觉模型没有返回可用的观察结果。",
                    metadata=metadata,
                )
            return ToolResult(
                title="Visual inspection",
                output=observation,
                metadata={
                    **metadata,
                    "vision_status": "completed",
                    "vision_model": vision_model_ref,
                    "image_path": str(image_path),
                    "mime_type": mime_type,
                    "source_model": current_model,
                },
            )
        except Exception:
            return self._unavailable(
                "已找到视觉模型，但本次视觉检查未能完成。",
                metadata={**metadata, "vision_model": vision_model_ref},
            )
        finally:
            if provider is not None:
                await self._close_provider(provider)

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
