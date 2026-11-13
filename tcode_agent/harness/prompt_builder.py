"""Prompt 组装与 LLM 调用。

这层只关心把 Agent 当前状态转换成模型输入，
尽量不处理工具执行或团队控制逻辑。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import hashlib
import uuid
from pathlib import Path
from typing import Any, Optional

from ..presentation import inspect_image_file, normalize_content_blocks
from ..context.builder import ModelContextAssembly, ModelContextBuilder, UserDirectiveBudgetExceeded
from ..checkpoint.manager import CheckpointManager
from ..checkpoint.milestone import save_milestone_checkpoint
from ..providers.base import ChatMessage, ChatResponse, ToolDefinition
from ..prompt.builder import ProjectPromptAssembler, latest_user_goal
from ..prompt.complete import CompletePromptRegistry
from ..prompt.composer import PromptComposer
from ..prompt.pipeline import PromptContributionPipeline
from ..prompt.security import quarantine_untrusted_content
from ..prompt.sections import (
    BuiltinPromptSectionRegistry,
    build_active_agent_context_candidate_input,
    build_direct_execution_mode_candidate_input,
    evaluate_public_action_commentary_activation,
)
from ..runtime.execution_profile import (
    get_execution_profile_char_budget,
    get_execution_profile_prompt_budget_tokens,
)
from ..vision import configured_model_supports_images
from ..runtime.computer import is_computer_tool


class AgentPromptBuilder:
    """把 Agent 的上下文整理成一次稳定的 LLM 调用。"""

    def __init__(self, agent: Any):
        """绑定 Agent 与唯一插件 Prompt 贡献管线。

        参数:
            agent: 提供运行上下文、消息和模型 Provider 的 Agent。
        """

        self.agent = agent
        self._contribution_pipeline = PromptContributionPipeline()

    async def _record_model_call(self, record: dict[str, Any]) -> None:
        """调用身份存入会话事件，不塞入模型上下文，也不覆盖会话元数据。"""
        from ..session.manager import SessionManager
        session_id = str(getattr(self.agent, "session_id", "") or "")
        if not session_id:
            return
        try:
            session = await SessionManager.get(session_id)
            if session is None:
                raise RuntimeError("model call session is not persisted")
            metadata = dict(getattr(session, "metadata", {}) or {})
            record = {**record, **{key: metadata[key] for key in
                      ("workflow_run_id", "workflow_stage_id") if metadata.get(key)}}
            await SessionManager.update(session_id, tenant_id=session.tenant_id, user_id=session.user_id,
                workspace_id=session.workspace_id, related_events=[{
                    "event_type": "model.call", "payload": record,
                    "idempotency_key": "model-call:" + record["call_id"] + ":" + record["status"]}])
        except Exception as exc:
            # 溯源存储故障显式告警，不修改模型结果或启动新调用。
            import warnings
            warnings.warn("model call provenance persistence failed: " + type(exc).__name__, RuntimeWarning)

    async def _provider_chat(self, **kwargs):
        """每次实际 Provider 调用单独记录，含既有输入恢复分支；不新增重试。"""
        from ..runtime.model_calls import new_model_call, finish_model_call
        record = new_model_call(self.agent.provider,
            session_id=str(getattr(self.agent, "session_id", "") or ""),
            stage=str(getattr(getattr(self.agent, "agent_info", None), "name", "") or ""))
        await self._record_model_call(record)
        try:
            response = await self.agent.provider.chat(**kwargs)
        except BaseException as exc:
            await self._record_model_call(finish_model_call(record, error=exc))
            raise
        await self._record_model_call(finish_model_call(record, response))
        return response

    def _complete_prompt(self, system_content: str) -> str:
        """通过唯一 CompletePrompt Provider 完成生产 Prompt。

        参数:
            system_content: PromptComposer 已完成预算组装的最终 system prompt。

        返回:
            Provider 返回的最终正文；当前内建 Provider 保持字节不变。
        """

        from ..presentation.result_selection import enabled, latest_selections, context_text
        if enabled():
            selections = latest_selections(getattr(self.agent, "messages", []))
            from ..presentation.research_compare import validate_selections
            selections = validate_selections(selections, str(getattr(self.agent, "workdir", "") or ""), getattr(self.agent, "_existing_session", None))
            if selections:
                system_content += "\n\n" + context_text(selections)
        complete = CompletePromptRegistry.assemble(system_content)
        self.agent._last_complete_prompt = {
            "provider_id": complete.provider_id,
            "provider_version": complete.provider_version,
            "content_digest": complete.content_digest,
            "char_count": complete.char_count,
            "byte_count": complete.byte_count,
            "assembly_phases": list(complete.assembly_phases),
        }
        return complete.content

    async def _compose_plugin_prompts(self, system_content: str) -> str:
        """把已准入插件贡献合并到生产 Prompt。

        参数:
            system_content: 已包含内建 Section 与运行时上下文的 Prompt。

        返回:
            插件贡献按稳定顺序追加后的正文。
        """

        composed, receipts = await self._contribution_pipeline.compose(
            system_content,
            workdir=str(getattr(self.agent, "workdir", "") or os.getcwd()),
            agent=str(
                getattr(getattr(self.agent, "agent_info", None), "name", "") or ""
            ),
            variables={
                "execution_profile": str(
                    getattr(self.agent, "_execution_profile", "") or ""
                ),
                "plan_mode": bool(getattr(self.agent, "_plan_mode", False)),
            },
            structured_output=isinstance(getattr(self.agent, "_output_format", None), dict)
                and self.agent._output_format.get("type") == "json_schema",
        )
        self.agent._last_prompt_contributions = [
            {
                "source": item.source,
                "name": item.name,
                "content_digest": item.content_digest,
                "char_count": item.char_count,
                "truncated": item.truncated,
                "required": item.required,
            }
            for item in receipts
        ]
        return composed

    async def _append_runtime_system_notice(self, text: str) -> None:
        content = str(text or "").strip()
        if not content:
            return

        from ..types import Message

        msg = Message(
            role="system",
            content=content,
            id=self.agent._generate_id(),
        )
        self.agent.messages.append(msg)

        existing_session = getattr(self.agent, "_existing_session", None)
        if existing_session is not None and hasattr(existing_session, "messages"):
            existing_session.messages = list(self.agent.messages)
            try:
                from ..session.manager import SessionManager

                await SessionManager.update(existing_session.id, messages=list(self.agent.messages))
            except Exception:
                pass

    @staticmethod
    def _is_input_too_large_error(exc: Exception) -> bool:
        return "LLM input too large before request" in str(exc or "")

    @staticmethod
    def _is_missing_user_role_error(exc: Exception) -> bool:
        text = str(exc or "")
        return "do not contain elements with the role of user" in text

    @staticmethod
    def _shorten_text(value: Any, *, limit: int) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 24)] + " ...[truncated]"

    @staticmethod
    def _assistant_content_for_prompt(message: Any) -> str:
        """Keep structured images understandable in later model turns."""
        content = str(getattr(message, "content", "") or "")
        blocks = normalize_content_blocks(
            getattr(message, "content_blocks", []) or []
        )
        if not blocks:
            return content

        image_lines: list[str] = []
        for block in blocks[:12]:
            label = str(block.get("caption") or block.get("alt") or "图片").strip()
            path = str(block.get("path") or "").strip()
            asset_id = str(block.get("asset_id") or "").strip()
            image_lines.append(
                f"- {label}: /workspace/{path} (asset_id={asset_id})"
            )
        presentation_note = (
            "[本条消息已向用户展示以下图片；用户后续说‘这张图’时可据此定位]\n"
            + "\n".join(image_lines)
        )
        return f"{content.rstrip()}\n\n{presentation_note}".strip()

    def _load_model_image(
        self,
        candidate: dict[str, Any],
        *,
        default_detail: str,
        maximum: int,
    ) -> Optional[dict[str, Any]]:
        raw_path = str(candidate.get("path") or "").strip()
        if not raw_path:
            return None
        try:
            inspected = inspect_image_file(Path(raw_path))
            image_path = Path(str(inspected["path"])).resolve()
            source = str(candidate.get("source") or "").strip() or "tool_observation"
            if source == "computer_observation":
                raw_root = str(candidate.get("artifact_root") or "").strip()
                if not raw_root:
                    return None
                image_path.relative_to(Path(raw_root).resolve())
            if source in {"user_upload", "paper_visual"}:
                raw_workdir = str(getattr(self.agent, "workdir", "") or "").strip()
                if not raw_workdir:
                    return None
                workdir = Path(raw_workdir).resolve()
                image_path.relative_to(workdir)
            if int(inspected["size"]) > maximum:
                return None
            expected_size = candidate.get("size")
            if expected_size not in (None, "") and int(expected_size) != int(inspected["size"]):
                return None
            expected_sha = str(candidate.get("sha256") or "").strip().lower()
            actual_sha = str(inspected.get("sha256") or "").strip().lower()
            if expected_sha and expected_sha != actual_sha:
                return None
            requested_mime = str(candidate.get("mime_type") or "").strip()
            mime = str(inspected["mime_type"] or requested_mime or "image/jpeg")
            if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                return None
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            return {
                "data_url": f"data:{mime};base64,{encoded}",
                "mime_type": mime,
                "detail": str(candidate.get("detail") or default_detail),
                "source": source,
                **({key: candidate.get(key) for key in ("reference_width", "reference_height", "observation_ref")}
                   if source == "computer_observation" else {}),
            }
        except (OSError, ValueError, TypeError):
            return None

    def _message_input_images_for_prompt(self, message: Any) -> list[dict[str, Any]]:
        maximum = self._env_int(
            "TCODE_USER_MODEL_IMAGE_MAX_BYTES",
            5 * 1024 * 1024,
            minimum=1024,
        )
        images: list[dict[str, Any]] = []
        for candidate in list(getattr(message, "input_images", []) or [])[:2]:
            if not isinstance(candidate, dict):
                continue
            loaded = self._load_model_image(
                {**candidate, "source": "user_upload"},
                default_detail="auto",
                maximum=maximum,
            )
            if loaded:
                images.append(loaded)
        return images

    def _configured_image_input_support(self) -> Optional[bool]:
        """Return the active model's declared image capability."""
        try:
            from ..config.manager import ConfigManager

            workdir = str(getattr(self.agent, "workdir", "") or os.getcwd())
            ConfigManager.load(workdir)
            config = ConfigManager.get_config_copy()
            model_ref = str(getattr(self.agent, "_model_ref", "") or "").strip()
            if not model_ref:
                provider = getattr(self.agent, "provider", None)
                provider_model = str(
                    getattr(provider, "_model", "")
                    or getattr(provider, "model", "")
                    or ""
                ).strip()
                matches: list[str] = []
                providers = config.get("provider")
                if provider_model and isinstance(providers, dict):
                    for provider_id, provider_cfg in providers.items():
                        models = (
                            provider_cfg.get("models")
                            if isinstance(provider_cfg, dict)
                            else None
                        )
                        if isinstance(models, dict) and provider_model in models:
                            matches.append(f"{provider_id}/{provider_model}")
                if len(matches) == 1:
                    model_ref = matches[0]
            if not model_ref:
                model_ref = str(config.get("model") or "").strip()
            return configured_model_supports_images(config, model_ref)
        except Exception:
            return None

    def _image_tool_fallback_note(self, message: Any) -> str:
        """Build safe workspace paths for a text model to pass to ``look_at``."""
        raw_workdir = str(getattr(self.agent, "workdir", "") or "").strip()
        if not raw_workdir:
            return ""
        try:
            workdir = Path(raw_workdir).resolve()
        except OSError:
            return ""

        paths: list[str] = []
        for candidate in list(getattr(message, "input_images", []) or [])[:2]:
            if not isinstance(candidate, dict):
                continue
            try:
                path = Path(str(candidate.get("path") or "")).resolve(strict=True)
                relative = path.relative_to(workdir).as_posix()
            except (OSError, ValueError):
                continue
            paths.append(f"/workspace/{relative}")
        if not paths:
            return ""
        listed = "\n".join(f"- {path}" for path in paths)
        return (
            "[IMAGE_INPUT_ROUTING]\n"
            "当前主模型被配置为不能直接接收图片；图片没有被丢弃，仍保存在当前会话工作区：\n"
            f"{listed}\n"
            "如果用户的问题依赖图片内容，先调用 look_at(path, question) 获取视觉观察，"
            "再结合用户问题作答。不要根据文件名猜测图片内容。若 look_at 返回不可用，"
            "请以正常的 Agent 回复说明本次无法完成视觉检查，并给出可执行的下一步；"
            "不要向用户展示供应商/API 原始报错。"
        )

    def _user_chat_message_for_prompt(self, message: Any) -> ChatMessage:
        user_images = self._message_input_images_for_prompt(message)
        user_content = str(getattr(message, "content", "") or "")
        if (getattr(message, "metadata", {}) or {}).get("turn_interruption"):
            user_content = (
                "[STOPPED REQUEST — HISTORY ONLY]\n"
                "This request was interrupted. Keep it as historical context, not an outstanding task. "
                "Do not execute or resume it unless the latest user request explicitly asks to do so.\n"
                + user_content
                + "\n[END STOPPED REQUEST]"
            )
        if user_images and self._configured_image_input_support() is not True:
            routing_note = self._image_tool_fallback_note(message)
            if routing_note:
                user_content = f"{user_content.rstrip()}\n\n{routing_note}".strip()
            user_images = []
        return ChatMessage(
            role="user",
            content=user_content,
            images=user_images or None,
        )

    def _tool_model_images_for_prompt(self, message: Any) -> list[dict[str, Any]]:
        """Load bounded model-visible images declared by trusted tool metadata."""
        candidates: list[dict[str, Any]] = []
        for tool_call in list(getattr(message, "tool_calls", []) or []):
            tool_name = str(
                getattr(tool_call, "tool_name", "")
                or (tool_call.get("tool_name", "") if isinstance(tool_call, dict) else "")
            ).strip()
            metadata = dict(
                getattr(tool_call, "metadata", {})
                or (tool_call.get("metadata", {}) if isinstance(tool_call, dict) else {})
                or {}
            )
            path = str(metadata.get("model_image_path") or "").strip()
            mime = str(metadata.get("model_image_mime") or "").strip()
            if tool_name == "browser" and path:
                candidates.append(
                    {
                        "path": path,
                        "mime_type": mime,
                        "detail": "low",
                        "source": "browser_observation",
                    }
                )
            for item in list(metadata.get("model_images") or [])[:2]:
                if isinstance(item, dict) and str(item.get("path") or "").strip():
                    candidates.append(dict(item))

        maximum = self._env_int(
            "TCODE_BROWSER_MODEL_IMAGE_MAX_BYTES",
            5 * 1024 * 1024,
            minimum=1024,
        )
        images: list[dict[str, Any]] = []
        for candidate in candidates[-2:]:
            loaded = self._load_model_image(
                candidate,
                default_detail="high" if candidate.get("source") == "paper_visual" else "low",
                maximum=maximum,
            )
            if loaded:
                images.append(loaded)
        return images

    def _tool_image_fallback_note(self, message: Any) -> str:
        """Route trusted tool images through ``look_at`` for text-only models."""
        raw_workdir = str(getattr(self.agent, "workdir", "") or "").strip()
        if not raw_workdir:
            return ""
        try:
            workdir = Path(raw_workdir).resolve()
        except OSError:
            return ""

        paths: list[str] = []
        for tool_call in list(getattr(message, "tool_calls", []) or []):
            metadata = dict(
                getattr(tool_call, "metadata", {})
                or (tool_call.get("metadata", {}) if isinstance(tool_call, dict) else {})
                or {}
            )
            for candidate in list(metadata.get("model_images") or [])[:2]:
                if not isinstance(candidate, dict):
                    continue
                try:
                    path = Path(str(candidate.get("path") or "")).resolve(strict=True)
                    relative = path.relative_to(workdir).as_posix()
                except (OSError, ValueError):
                    continue
                workspace_path = f"/workspace/{relative}"
                if workspace_path not in paths:
                    paths.append(workspace_path)
        if not paths:
            return ""
        listed = "\n".join(f"- {path}" for path in paths)
        return (
            "[IMAGE_TOOL_ROUTING]\n"
            "上述工具生成了视觉证据，但当前主模型不能直接接收图片。"
            "请逐张调用 look_at，并把得到的视觉观察与上述结构化元数据、原生解析结果结合：\n"
            f"{listed}\n"
            "若视觉检查不可用，保留已有确定性解析结果并清楚说明视觉证据尚未核验；"
            "不要输出底层供应商/API 原始报错。"
        )

    def _tool_chat_message_for_prompt(
        self,
        message: Any,
        *,
        tool_call_id: Optional[str],
    ) -> ChatMessage:
        tool_names = [
            str(
                getattr(tool_call, "tool_name", "")
                or (tool_call.get("tool_name", "") if isinstance(tool_call, dict) else "")
            ).strip()
            for tool_call in list(getattr(message, "tool_calls", []) or [])
        ]
        source = "tool_result:" + (",".join(name for name in tool_names if name) or "unknown")
        raw_content = self._format_tool_result_for_prompt(message.content)
        content, scan = quarantine_untrusted_content(raw_content, source=source)
        if scan.blocked:
            scans = list(getattr(self.agent, "_last_untrusted_content_scans", []) or [])
            scans.append(
                {
                    "source": scan.source,
                    "findings": [finding.kind for finding in scan.findings],
                }
            )
            self.agent._last_untrusted_content_scans = scans[-32:]
        images = self._tool_model_images_for_prompt(message)
        if images and self._configured_image_input_support() is not True:
            if any(is_computer_tool(name) for name in tool_names):
                raise ValueError("当前模型未明确声明支持图像，无法接收桌面截图；请选择已配置且获准接收截图的视觉模型。")
            routing_note = self._tool_image_fallback_note(message)
            if routing_note:
                content = f"{content.rstrip()}\n\n{routing_note}".strip()
            images = []
        is_error = None
        for tool_call in list(getattr(message, "tool_calls", []) or []):
            state = getattr(tool_call, "state", None)
            state_value = str(getattr(state, "value", state) or "").lower()
            if str(getattr(tool_call, "error", "") or "").strip() or state_value in {
                "error",
                "interrupted",
            }:
                is_error = True
                break
        if tool_call_id and not scan.blocked and getattr(self.agent, '_goal_tool_routing_prompt', ''):
            from ..runtime.goal_evidence import is_observed_record
            from ..tools.builtin.goal_checkpoint import goal_checkpoint_active
            if goal_checkpoint_active.get() and any(is_observed_record(call) for call in message.tool_calls or []):
                content = f"[目标证据记录标识: {tool_call_id}]\n{content}"
        return ChatMessage(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            images=images or None,
            is_error=is_error,
        )

    @staticmethod
    def _expand_tool_model_images(messages: list[ChatMessage]) -> list[ChatMessage]:
        """Place images after complete tool-result groups without breaking tool ordering."""
        image_candidates = [
            image
            for message in messages
            if str(getattr(message, "role", "") or "") == "tool"
            for image in list(getattr(message, "images", None) or [])
        ]
        latest_computer = next((image for image in reversed(image_candidates)
                                if image.get("source") == "computer_observation"), None)
        observation_calls = {
            call.get("id")
            for message in messages
            for call in list(getattr(message, "tool_calls", None) or [])
            if dict(call.get("function") or {}).get("name") in {"computer_observe", "computer"}
        }
        latest_observation = next((message for message in reversed(messages)
                                   if message.role == "tool" and message.tool_call_id in observation_calls), None)
        if latest_observation is not None and not any(
            image.get("source") == "computer_observation" for image in latest_observation.images or []
        ):
            latest_computer = None
        selected = {id(image) for image in image_candidates[-2:]
                    if image.get("source") != "computer_observation" or image is latest_computer}
        expanded: list[ChatMessage] = []
        pending: list[dict[str, Any]] = []

        def flush() -> None:
            if not pending:
                return
            expanded.append(
                ChatMessage(
                    role="user",
                    content=(
                        "Model-visible image evidence from the immediately preceding tool result. "
                        "Inspect it together with the structured evidence and preserve page/table/figure provenance."
                    ) + "".join(
                        f"\nDesktop observation {image.get('observation_ref')}: "
                        f"reference_width={image.get('reference_width')}, reference_height={image.get('reference_height')}. "
                        "Locate relative to this entire image; prefer normalized x/y in 0..1. "
                        "Never use coordinates from a resized display or an earlier screenshot."
                        for image in pending if image.get("source") == "computer_observation"
                    ),
                    images=list(pending),
                )
            )
            pending.clear()

        for message in messages:
            role = str(getattr(message, "role", "") or "")
            images = [
                image
                for image in list(getattr(message, "images", None) or [])
                if id(image) in selected
            ]
            if role == "tool":
                message.images = None
                expanded.append(message)
                pending.extend(images)
                continue
            flush()
            expanded.append(message)
        flush()
        return expanded

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
        raw = str(os.environ.get(name, "") or "").strip()
        if not raw:
            return max(minimum, default)
        try:
            return max(minimum, int(raw))
        except Exception:
            return max(minimum, default)

    def _oversized_message_chars(self) -> int:
        base = self._env_int("TCODE_EMERGENCY_COMPACT_MESSAGE_CHARS", 80_000, minimum=8_000)
        return get_execution_profile_char_budget(
            getattr(self.agent, "_execution_profile", None),
            "emergency_message_chars",
            base,
            minimum=8_000,
        )

    def _retry_total_chars(self) -> int:
        base = self._env_int("TCODE_LLM_RETRY_MAX_MESSAGE_CHARS", 120_000, minimum=20_000)
        return get_execution_profile_char_budget(
            getattr(self.agent, "_execution_profile", None),
            "retry_total_chars",
            base,
            minimum=20_000,
        )

    def _retry_per_message_chars(self) -> int:
        base = self._env_int("TCODE_LLM_RETRY_MAX_SINGLE_MESSAGE_CHARS", 16_000, minimum=2_000)
        return get_execution_profile_char_budget(
            getattr(self.agent, "_execution_profile", None),
            "retry_per_message_chars",
            base,
            minimum=2_000,
        )

    def _tool_argument_prompt_chars(self) -> int:
        return self._env_int("TCODE_LLM_TOOL_ARGUMENT_MAX_CHARS", 12_000, minimum=512)

    def _tool_result_prompt_chars(self) -> int:
        return self._env_int("TCODE_LLM_TOOL_RESULT_MAX_CHARS", 40_000, minimum=1_000)

    def _format_tool_arguments_for_prompt(self, arguments: Any) -> str:
        try:
            text = json.dumps(arguments or {}, ensure_ascii=False)
        except Exception:
            text = json.dumps({"value": str(arguments or {})}, ensure_ascii=False)

        limit = self._tool_argument_prompt_chars()
        if len(text) <= limit:
            return text

        preview = self._shorten_text(text, limit=limit)
        compacted = {
            "__tcode_truncated_tool_arguments__": True,
            "original_chars": len(text),
            "preview": preview,
        }
        return json.dumps(compacted, ensure_ascii=False)

    def _format_tool_result_for_prompt(self, content: Any) -> str:
        text = str(content or "")
        limit = self._tool_result_prompt_chars()
        if len(text) <= limit:
            return text

        # Discovery is paginated and contains opaque capabilities. Character
        # slicing can corrupt JSON or remove the only actionable target ref.
        try:
            structured = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            structured = None
        if isinstance(structured, dict) and structured.get("operation") in {"list_apps", "list_windows"}:
            return json.dumps(structured, ensure_ascii=False, separators=(",", ":"))

        preview = self._shorten_text(text, limit=limit)
        return (
            f"{preview}\n\n"
            f"[tool output truncated for prompt: original_chars={len(text)}, shown_chars={len(preview)}; "
            "request a narrower read or use artifact paths for omitted content.]"
        )

    @staticmethod
    def _normalize_tool_arguments(
        arguments: Any,
        schema: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        """Decode provider tool arguments without letting malformed JSON abort a turn.

        Tool arguments are an execution boundary. Unmarked tools remain strict.
        Opted-in plugin schemas may restore exactly one missing object close only
        when the complete schema admits a unique interpretation, without changing
        any token or value. Otherwise return a safe empty argument object plus
        structured diagnostics.  The caller may ask the model to regenerate the call;
        if regeneration still fails, the executor records a non-executed tool error.
        """

        if arguments in (None, ""):
            return {}, None
        if isinstance(arguments, dict):
            return dict(arguments), None

        raw = arguments if isinstance(arguments, str) else ""
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                if schema and schema.get("x-tcode-argument-format") == "v1":
                    from ..plugin_sdk.argument_format import restore_missing_object_close
                    restored = restore_missing_object_close(raw, schema)
                    if restored is not None:
                        return restored, None
                return {}, {
                    "code": "invalid_tool_arguments_json",
                    "message": "Tool arguments must be one complete JSON object.",
                    "parser_message": str(exc.msg or "invalid JSON"),
                    "line": int(exc.lineno or 0),
                    "column": int(exc.colno or 0),
                    "position": int(exc.pos or 0),
                    "original_chars": len(raw),
                    "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                    "retryable": True,
                }
            except (TypeError, ValueError) as exc:
                return {}, {
                    "code": "invalid_tool_arguments_json",
                    "message": "Tool arguments must be one complete JSON object.",
                    "parser_message": type(exc).__name__,
                    "original_chars": len(raw),
                    "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                    "retryable": True,
                }
            if isinstance(parsed, dict):
                return parsed, None
            return {}, {
                "code": "invalid_tool_arguments_type",
                "message": "Tool arguments JSON must decode to an object.",
                "decoded_type": type(parsed).__name__,
                "original_chars": len(raw),
                "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                "retryable": True,
            }

        return {}, {
            "code": "invalid_tool_arguments_type",
            "message": "Tool arguments must be a JSON object.",
            "decoded_type": type(arguments).__name__,
            "retryable": True,
        }

    @staticmethod
    def _invalid_tool_arguments_prompt_value(error: dict[str, Any]) -> dict[str, Any]:
        """Keep historical malformed calls provider-valid without retaining raw text."""

        return {
            "__tcode_invalid_tool_arguments__": {
                key: value
                for key, value in dict(error or {}).items()
                if key not in {"sha256"}
            }
        }

    def _provider_max_input_chars(self) -> int:
        base = 0
        limit_getter = getattr(self.agent.provider, "_max_input_chars", None)
        if callable(limit_getter):
            try:
                base = max(10_000, int(limit_getter()))
            except Exception:
                pass
        if not base:
            base = self._env_int("TCODE_LLM_PREFLIGHT_MAX_INPUT_CHARS", 250_000, minimum=10_000)
        return get_execution_profile_char_budget(
            getattr(self.agent, "_execution_profile", None),
            "max_input_chars",
            base,
            minimum=10_000,
        )

    def _configured_max_output_tokens(self) -> int:
        """Return an optional per-agent provider output cap.

        Plugin agents can emit large structured tool arguments.  Keeping this
        opt-in on ``AgentInfo.options`` lets a plugin bound one expensive
        stage without changing the default behavior of unrelated agents.
        """

        options = getattr(getattr(self.agent, "agent_info", None), "options", {}) or {}
        raw = options.get("max_output_tokens") if isinstance(options, dict) else None
        if raw in (None, ""):
            return 0
        try:
            return max(512, min(int(raw), 131_072))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _reject_truncated_response(response: Any) -> None:
        """Never execute partial calls or synthesize success from a cut-off response."""

        reason = str(getattr(response, "finish_reason", "") or "").strip().lower()
        if reason in {"length", "max_tokens"}:
            raise RuntimeError(
                "LLM output budget exhausted "
                f"(finish_reason={reason}); no tool from this response was executed. "
                "Check reasoning effort and output size before rerunning; "
                "do not repeat the unchanged request."
            )

    @staticmethod
    def _append_without_repeated_boundary(current: str, continuation: str) -> str:
        """Join continuation text without duplicating an exact boundary."""

        left = str(current or "")
        right = str(continuation or "")
        overlap_limit = min(2_000, len(left), len(right))
        for size in range(overlap_limit, 0, -1):
            if left[-size:] == right[:size]:
                return left + right[size:]
        if left and right and not left.endswith(("\n", " ")) and not right.startswith(("\n", " ")):
            return left + "\n" + right
        return left + right

    async def _continue_truncated_text(
        self,
        response: ChatResponse,
        *,
        messages_for_api: list[ChatMessage],
        max_input_chars: int,
    ) -> ChatResponse:
        """Continue cut-off prose; partial tool calls remain forbidden."""

        reason = str(getattr(response, "finish_reason", "") or "").strip().lower()
        if reason not in {"length", "max_tokens"}:
            return response
        if getattr(response, "tool_calls", None):
            self._reject_truncated_response(response)

        rounds = self._env_int("TCODE_LLM_OUTPUT_CONTINUATION_ROUNDS", 2, minimum=0)
        rounds = min(rounds, 5)
        max_chars = self._env_int("TCODE_LLM_OUTPUT_CONTINUATION_MAX_CHARS", 80_000, minimum=4_000)
        combined = str(getattr(response, "content", "") or "")
        usage = dict(getattr(response, "usage", None) or {})
        latest = response

        for _ in range(rounds):
            if len(combined) >= max_chars:
                break
            continuation_input = self._trim_chat_messages_for_retry(
                [
                    *messages_for_api[-8:],
                    ChatMessage(role="assistant", content=combined[-16_000:]),
                    ChatMessage(
                        role="user",
                        content=(
                            "Continue exactly where the previous answer stopped. "
                            "Do not repeat earlier text. Do not call tools. Finish the answer concisely."
                        ),
                    ),
                ],
                max_total_chars=max(4_000, int(max_input_chars * 0.65)),
            )
            latest = await self._provider_chat(
                messages=self._ensure_user_role_for_provider(continuation_input),
                tools=None,
                temperature=self.agent.agent_info.temperature,
                reasoning_effort=self.agent._reasoning_effort,
                max_tokens=self._configured_max_output_tokens() or None,
            )
            if getattr(latest, "tool_calls", None):
                raise RuntimeError(
                    "LLM returned a tool call while continuing truncated prose; "
                    "no tool from this response was executed."
                )
            combined = self._append_without_repeated_boundary(
                combined,
                str(getattr(latest, "content", "") or ""),
            )[:max_chars]
            for key, value in dict(getattr(latest, "usage", None) or {}).items():
                if isinstance(value, (int, float)):
                    usage[key] = int(usage.get(key, 0) or 0) + int(value)
            if str(getattr(latest, "finish_reason", "") or "").strip().lower() not in {"length", "max_tokens"}:
                return ChatResponse(
                    content=combined,
                    tool_calls=None,
                    finish_reason=getattr(latest, "finish_reason", None),
                    usage=usage or None,
                    reasoning_content=getattr(response, "reasoning_content", None),
                    reasoning_blocks=getattr(response, "reasoning_blocks", None),
                )

        marker = "\n\n[输出因累计预算达到上限而停止；以上为已保留的部分结果。]"
        return ChatResponse(
            content=combined.rstrip() + marker,
            tool_calls=None,
            finish_reason="partial_output_budget",
            usage=usage or None,
            reasoning_content=getattr(response, "reasoning_content", None),
            reasoning_blocks=getattr(response, "reasoning_blocks", None),
        )

    @staticmethod
    def _estimate_tool_chars(tools: Optional[list[ToolDefinition]]) -> int:
        if not tools:
            return 0
        total = 0
        for tool in tools:
            total += len(str(getattr(tool, "name", "") or ""))
            total += len(str(getattr(tool, "description", "") or ""))
            try:
                total += len(json.dumps(getattr(tool, "parameters", {}) or {}, ensure_ascii=False))
            except Exception:
                total += len(str(getattr(tool, "parameters", {}) or ""))
        return total

    def _estimate_chat_payload_chars(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]],
    ) -> int:
        total = self._estimate_tool_chars(tools)
        for msg in messages or []:
            total += len(str(getattr(msg, "role", "") or ""))
            total += len(str(getattr(msg, "content", "") or ""))
            total += len(str(getattr(msg, "tool_call_id", "") or ""))
            for tc in getattr(msg, "tool_calls", None) or []:
                try:
                    total += len(json.dumps(tc, ensure_ascii=False))
                except Exception:
                    total += len(str(tc))
        return total

    async def _call_degraded_llm(
        self,
        *,
        messages_for_api: list[ChatMessage],
        max_input_chars: int,
    ):
        degraded_system = ChatMessage(
            role="system",
            content=(
                "Context is too large. Temporarily disable tool calls for this turn. "
                "Respond concisely, ask user to narrow scope or request specific files."
            ),
        )
        retry_budget = max(4_000, int(max_input_chars * 0.65))
        goal_anchor = self._missing_goal_anchor([])
        if goal_anchor:
            retry_budget -= len(goal_anchor[0].content)
            if retry_budget <= 0:
                raise RuntimeError("Active goal instructions exceed the context retry budget; cannot continue with partial instructions")
        degraded_messages = [
            degraded_system,
            *goal_anchor,
            *self._trim_chat_messages_for_retry(
                [message for message in messages_for_api[-12:] if message not in goal_anchor],
                max_total_chars=retry_budget,
                per_message_limit=min(self._retry_per_message_chars(), max(2_000, retry_budget // 4)),
            ),
        ]
        response = await self._provider_chat(
            messages=self._ensure_user_role_for_provider(degraded_messages),
            tools=None,
            temperature=self.agent.agent_info.temperature,
            reasoning_effort=self.agent._reasoning_effort,
            max_tokens=self._configured_max_output_tokens() or None,
        )
        await self._append_runtime_system_notice(
            "[LLM_INPUT_GUARD] Oversized input detected; this turn was retried in degraded mode without tools."
        )
        return response

    def _missing_goal_anchor(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        from ..tools.builtin.goal_checkpoint import goal_checkpoint_active

        goal = getattr(self.agent, "_goal_tool_routing_prompt", "") if goal_checkpoint_active.get() else ""
        if not goal or any(message.role == "user" and message.content == goal for message in messages):
            return []
        # Keep the original user authority; never elevate the objective to a system instruction.
        return [ChatMessage(role="user", content=goal)]

    def _trim_chat_messages_for_retry(
        self,
        messages: list[ChatMessage],
        *,
        max_total_chars: Optional[int] = None,
        per_message_limit: Optional[int] = None,
    ) -> list[ChatMessage]:
        max_total = max_total_chars or self._retry_total_chars()
        per_message = per_message_limit or self._retry_per_message_chars()
        remaining = max_total
        selected: list[ChatMessage] = []

        for msg in reversed(messages or []):
            role = msg.role if msg.role in {"system", "user", "assistant"} else "system"
            prefix = "" if role == msg.role else f"[{msg.role} message converted for oversized retry]\n"
            content = prefix + str(msg.content or "")
            if remaining <= 0:
                break
            limit = min(per_message, remaining)
            trimmed = self._shorten_text(content, limit=limit)
            remaining -= len(trimmed)
            selected.append(
                ChatMessage(
                    role=role,
                    content=trimmed,
                    tool_calls=None,
                    tool_call_id=None,
                )
            )

        selected.reverse()
        return selected

    def _ensure_user_role_for_provider(
        self,
        messages: list[ChatMessage],
    ) -> list[ChatMessage]:
        has_user = any(
            str(getattr(msg, "role", "") or "") == "user"
            and bool(str(getattr(msg, "content", "") or "").strip())
            for msg in (messages or [])
        )
        if has_user:
            return list(messages or [])

        fallback_user = "请基于当前上下文继续执行，并明确下一步需要的最小信息。"
        for msg in reversed(getattr(self.agent, "messages", []) or []):
            if str(getattr(msg, "role", "") or "") != "user":
                continue
            content = str(getattr(msg, "content", "") or "").strip()
            if content:
                fallback_user = self._shorten_text(content, limit=self._retry_per_message_chars())
                break

        return [
            *list(messages or []),
            ChatMessage(role="user", content=fallback_user),
        ]

    @staticmethod
    def _chat_tool_call_id(tool_call: Any) -> str:
        if not isinstance(tool_call, dict):
            return ""
        return str(tool_call.get("id", "") or "").strip()

    @staticmethod
    def _chat_tool_call_name(tool_call: Any) -> str:
        if not isinstance(tool_call, dict):
            return ""
        function = (
            tool_call.get("function")
            if isinstance(tool_call.get("function"), dict)
            else {}
        )
        return str(function.get("name", "") or "").strip()

    def _assistant_tool_calls_as_context(
        self,
        msg: ChatMessage,
        *,
        missing_ids: set[str],
    ) -> ChatMessage:
        lines = []
        content = str(getattr(msg, "content", "") or "").strip()
        if content:
            lines.append(content)
            lines.append("")
        lines.append(
            "[tool-call history note] The assistant previously requested tool calls, "
            "but their tool results were not complete and contiguous in the saved history. "
            "They are shown as context instead of active tool calls."
        )
        for tool_call in getattr(msg, "tool_calls", None) or []:
            call_id = self._chat_tool_call_id(tool_call)
            name = self._chat_tool_call_name(tool_call) or "unknown_tool"
            status = (
                "missing_result"
                if call_id in missing_ids
                else "result_not_attached"
            )
            id_suffix = f" id={call_id}" if call_id else ""
            lines.append(f"- {name}{id_suffix} ({status})")
        return ChatMessage(
            role="assistant",
            content="\n".join(lines),
            tool_calls=None,
            reasoning_content=msg.reasoning_content,
        )

    def _tool_message_as_context(self, msg: ChatMessage) -> ChatMessage:
        call_id = str(getattr(msg, "tool_call_id", "") or "").strip()
        id_line = f"tool_call_id: {call_id}\n" if call_id else ""
        return ChatMessage(
            role="user",
            content=(
                "[tool-result history note]\n"
                f"{id_line}"
                f"{str(getattr(msg, 'content', '') or '')}"
            ).strip(),
            images=list(getattr(msg, "images", None) or []) or None,
        )

    def _normalize_tool_call_message_chain(
        self,
        messages: list[ChatMessage],
    ) -> list[ChatMessage]:
        """Return a provider-safe history for strict tool-call message ordering.

        OpenAI requires an assistant message with tool_calls to be followed
        immediately by one tool message for each tool_call_id. Direct user
        guidance, interrupted turns, and internal tool-like context can leave
        saved history in a shape that is useful locally but invalid for the API.
        """
        normalized: list[ChatMessage] = []
        idx = 0
        items = list(messages or [])

        while idx < len(items):
            msg = items[idx]
            role = str(getattr(msg, "role", "") or "")

            if role == "tool":
                normalized.append(self._tool_message_as_context(msg))
                idx += 1
                continue

            tool_calls = list(getattr(msg, "tool_calls", None) or [])
            if role != "assistant" or not tool_calls:
                normalized.append(msg)
                idx += 1
                continue

            call_ids = [
                call_id
                for call_id in (self._chat_tool_call_id(tc) for tc in tool_calls)
                if call_id
            ]
            expected_ids = set(call_ids)
            if not expected_ids or len(expected_ids) != len(call_ids):
                normalized.append(
                    self._assistant_tool_calls_as_context(
                        msg,
                        missing_ids=expected_ids,
                    )
                )
                idx += 1
                continue

            cursor = idx + 1
            attached_tool_messages: list[ChatMessage] = []
            attached_ids: set[str] = set()
            context_tool_messages: list[ChatMessage] = []
            while cursor < len(items):
                next_msg = items[cursor]
                if str(getattr(next_msg, "role", "") or "") != "tool":
                    break
                tool_call_id = str(getattr(next_msg, "tool_call_id", "") or "").strip()
                if tool_call_id in expected_ids and tool_call_id not in attached_ids:
                    attached_tool_messages.append(next_msg)
                    attached_ids.add(tool_call_id)
                else:
                    context_tool_messages.append(next_msg)
                cursor += 1

            missing_ids = expected_ids - attached_ids
            if missing_ids:
                normalized.append(
                    self._assistant_tool_calls_as_context(
                        msg,
                        missing_ids=missing_ids,
                    )
                )
                for tool_msg in [*attached_tool_messages, *context_tool_messages]:
                    normalized.append(self._tool_message_as_context(tool_msg))
            else:
                normalized.append(msg)
                normalized.extend(attached_tool_messages)
                for tool_msg in context_tool_messages:
                    normalized.append(self._tool_message_as_context(tool_msg))

            idx = cursor

        return normalized

    @staticmethod
    def _emergency_message_content(message: Any) -> str:
        content = str(getattr(message, "content", "") or "")
        if not content and str(getattr(message, "role", "") or "") == "tool":
            # Persisted tool observations live in ToolCall.output, not Message.content.
            # Flatten before clearing tool_calls so emergency compaction keeps evidence.
            observations = []
            for call in getattr(message, "tool_calls", []) or []:
                observations.append(json.dumps({
                    "tool": getattr(call, "tool_name", ""),
                    "state": getattr(call, "state", ""),
                    "output": getattr(call, "output", ""),
                    "error": getattr(call, "error", None),
                }, ensure_ascii=False))
            content = "\n".join([content, *observations]).strip()
        return content

    async def _emergency_compact_agent_messages(self) -> bool:
        messages = list(getattr(self.agent, "messages", []) or [])
        if (
            messages
            and str(getattr(messages[0], "role", "") or "") == "system"
            and "[EMERGENCY CONTEXT COMPACTION]" in str(getattr(messages[0], "content", "") or "")
            and len(messages) <= 9
        ):
            return False
        total_chars = sum(len(str(getattr(msg, "content", "") or "")) for msg in messages)
        if len(messages) <= 8 and total_chars < self._oversized_message_chars():
            return False

        keep_recent = 8 if len(messages) > 8 else min(4, len(messages))
        old = messages[:-keep_recent]
        recent = messages[-keep_recent:]
        existing_session = getattr(self.agent, "_existing_session", None)
        lifecycle_payload: dict[str, Any] | None = None
        if existing_session is not None:
            from ..session.compaction import SessionCompaction

            lifecycle_payload = {
                "operation_id": uuid.uuid4().hex,
                "session_id": str(getattr(existing_session, "id", "") or ""),
                "agent": str(
                    getattr(getattr(self.agent, "agent_info", None), "name", "") or ""
                ),
                "trigger": "emergency",
                "message_count": len(old),
                "messages": SessionCompaction._lifecycle_messages(old),
            }
            await SessionCompaction._emit_lifecycle(
                existing_session,
                "session.compaction.before",
                lifecycle_payload,
            )

        summary_lines: list[str] = [
            "[EMERGENCY CONTEXT COMPACTION] 历史消息过长，已自动压缩早期上下文。",
            "请优先基于最近消息继续执行，必要时再请求具体文件或产物。",
            "",
            "已压缩历史摘要:",
        ]
        snippet_count = 0
        for msg in old:
            role = str(getattr(msg, "role", "") or "").strip() or "unknown"
            content = self._shorten_text(self._emergency_message_content(msg), limit=600)
            if not content:
                continue
            summary_lines.append(f"- [{role}] {content}")
            snippet_count += 1
            if snippet_count >= 10:
                break
        if snippet_count == 0:
            summary_lines.append("- [system] 早期消息已压缩（无可用摘要片段）。")

        from ..types import Message

        summary_msg = Message(
            role="system",
            content="\n".join(summary_lines),
            id=self.agent._generate_id(),
            summary=True,
        )
        recent_limit = self._retry_per_message_chars()
        trimmed_recent: list[Message] = []
        for msg in recent:
            role = str(getattr(msg, "role", "") or "system").strip() or "system"
            content = self._shorten_text(self._emergency_message_content(msg), limit=recent_limit)
            if role == "tool":
                role = "system"
                content = "[tool result converted during emergency compaction]\n" + content
            trimmed_recent.append(
                Message(
                    role=role,
                    content=content,
                    id=str(getattr(msg, "id", "") or self.agent._generate_id()),
                    parent_id=getattr(msg, "parent_id", None),
                    tool_calls=[] if role == "system" else list(getattr(msg, "tool_calls", []) or []),
                    usage=getattr(msg, "usage", None),
                    content_blocks=list(getattr(msg, "content_blocks", []) or []),
                )
            )

        if not any(str(getattr(msg, "role", "") or "") == "user" for msg in trimmed_recent):
            fallback_user = "请基于当前上下文继续执行，并在信息不足时给出最小补充请求。"
            for msg in reversed(messages):
                if str(getattr(msg, "role", "") or "") != "user":
                    continue
                content = str(getattr(msg, "content", "") or "").strip()
                if content:
                    fallback_user = self._shorten_text(content, limit=recent_limit)
                    break
            trimmed_recent.append(
                Message(
                    role="user",
                    content=fallback_user,
                    id=self.agent._generate_id(),
                )
            )

        new_messages = [summary_msg, *trimmed_recent]
        self.agent.messages = new_messages

        if existing_session is not None and hasattr(existing_session, "messages"):
            if not getattr(existing_session, "display_messages", None):
                existing_session.display_messages = list(existing_session.messages)
            existing_session.messages = list(new_messages)
            try:
                from ..session.manager import SessionManager

                await SessionManager.update(
                    existing_session.id,
                    messages=list(new_messages),
                    display_messages=existing_session.display_messages,
                )
            except Exception:
                pass

        if existing_session is not None and lifecycle_payload is not None:
            from ..session.compaction import SessionCompaction

            await SessionCompaction._emit_lifecycle(
                existing_session,
                "session.compaction.after",
                {
                    **lifecycle_payload,
                    "messages": [],
                    "status": "success",
                    "summary": str(summary_msg.content or "")[:8000],
                    "original_message_count": len(messages),
                    "retained_message_count": len(new_messages),
                    "fallback": True,
                },
                mode="parallel",
            )

        return True

    async def _ensure_plugin_team_catalog_loaded(self) -> None:
        from ..plugins.manager import PluginManager

        candidates = [
            str(os.environ.get("TCODE_WORKDIR", "") or "").strip(),
            os.getcwd(),
            str(getattr(self.agent, "workdir", "") or "").strip(),
        ]
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                await PluginManager.get_global(candidate)
            except Exception:
                continue

    async def _build_manager_team_snapshot_section(self) -> str:
        agent_info = getattr(self.agent, "agent_info", None)
        mode_obj = getattr(agent_info, "mode", "")
        mode_value = str(getattr(mode_obj, "value", mode_obj) or "").strip().lower()
        if mode_value != "manager":
            return ""

        try:
            await self._ensure_plugin_team_catalog_loaded()

            from ..agent.agents import BuiltinAgents
            from ..team.catalog import TeamCatalog
            from ..team.protein_team_config import load_protein_team_config

            all_entries = TeamCatalog.list(available_only=False)
            if not all_entries:
                return ""

            lines = [
                "当前已注册团队快照（运行时动态）：",
                "- 你可以用 list_teams 查看完整目录，用 list_workers 查看当前会话/委派子会话的 Worker 运行状态。",
            ]
            for entry in all_entries:
                available = BuiltinAgents.get(entry.runtime_agent) is not None
                status = "available" if available else "unavailable"
                reason = str(entry.delegation_reason or "").strip()
                line = (
                    f"- {entry.display_name} | key={entry.key} | runtime={entry.runtime_agent} | status={status}"
                )
                if reason:
                    line = line + f" | reason={reason}"
                lines.append(line)

                expected_members: list[str] = []
                if str(entry.key or "").strip().lower() == "protein":
                    cfg = load_protein_team_config()
                    for item in cfg.get("workers", []):
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", "") or "").strip()
                        role = str(item.get("role", "") or "").strip()
                        if not name:
                            continue
                        expected_members.append(f"{name}({role})" if role else name)
                elif str(entry.key or "").strip().lower() == "paper":
                    for agent in BuiltinAgents.list():
                        name = str(getattr(agent, "name", "") or "").strip()
                        if not name.startswith("paper_") or name == "paper_auto_coordinator":
                            continue
                        desc = str(getattr(agent, "description", "") or "").strip()
                        expected_members.append(f"{name}({desc})" if desc else name)
                else:
                    for worker in getattr(entry, "workers", ()) or ():
                        if not bool(getattr(worker, "enabled", True)):
                            continue
                        if not bool(getattr(worker, "visible", True)):
                            continue
                        name = str(getattr(worker, "name", "") or "").strip()
                        role = str(getattr(worker, "role", "") or "").strip()
                        if not name:
                            continue
                        expected_members.append(f"{name}({role})" if role else name)

                if expected_members:
                    preview = ", ".join(expected_members[:8])
                    if len(expected_members) > 8:
                        preview = preview + f", ...(+{len(expected_members) - 8})"
                    lines.append(f"  预置成员模板: {preview}")

            return "\n".join(lines)
        except Exception:
            return ""

    def _build_active_agent_context_section(self) -> str:
        """按既有运行时语义生成 Active Agent Context Legacy 正文。

        返回:
            Agent 名称存在且动态 Agent/Team 来源读取成功时返回原有英文
            正文；名称缺失或来源异常时返回空字符串。

        本函数继续独立读取 BuiltinAgents 与 TeamCatalog，不与第四 Section
        候选输入构建器或候选 renderer 共享动态结果，确保 Shadow 能发现
        任一单侧来源、过滤或格式漂移。
        """

        agent_info = getattr(self.agent, "agent_info", None)
        agent_name = str(getattr(agent_info, "name", "") or "").strip()
        if not agent_name:
            return ""

        try:
            from ..agent.agents import BuiltinAgents
            from ..team.catalog import TeamCatalog

            display_name = agent_name
            agent_options = getattr(agent_info, "options", {}) or {}
            if isinstance(agent_options, dict):
                display_name = str(agent_options.get("display_name") or display_name).strip()
            info = BuiltinAgents.get(agent_name)
            if info is not None:
                options = getattr(info, "options", {}) or {}
                display_name = str(
                    options.get("display_name") or display_name or info.name
                ).strip()

            lines = [
                f"Current active agent: `{agent_name}` ({display_name or agent_name}).",
                (
                    "Agent/mode changes are UI state, not user tasks. Do not treat "
                    "a mode change itself as a request to initialize, inspect memory, "
                    "scan files, or check processes."
                ),
                (
                    "Answer subsequent user messages from this active agent identity, "
                    "not from a previous mode."
                ),
            ]

            entry = TeamCatalog.get_by_agent_name(agent_name, available_only=False)
            if entry is not None:
                members: list[str] = []
                for worker in getattr(entry, "workers", ()) or ():
                    if not bool(getattr(worker, "enabled", True)):
                        continue
                    if not bool(getattr(worker, "visible", True)):
                        continue
                    name = str(getattr(worker, "name", "") or "").strip()
                    if not name:
                        continue
                    role = str(getattr(worker, "role", "") or "").strip()
                    if role.lower() == name.lower():
                        role = ""
                    qualifiers: list[str] = []
                    if role:
                        qualifiers.append(role)
                    if bool(getattr(worker, "resident", False)):
                        qualifiers.append("resident")
                    members.append(
                        f"{name}({', '.join(qualifiers)})" if qualifiers else name
                    )

                if not members:
                    members = [
                        str(name or "").strip()
                        for name in getattr(entry, "worker_names", ()) or ()
                        if str(name or "").strip()
                    ]

                lines.append(
                    "Active team: "
                    f"key={entry.key}, display_name={entry.display_name}, "
                    f"runtime_agent={entry.runtime_agent}."
                )
                if members:
                    preview = ", ".join(members[:12])
                    if len(members) > 12:
                        preview += f", ...(+{len(members) - 12})"
                    lines.append(
                        f"Visible configured members: {len(members)}; {preview}."
                    )
                    lines.append(
                        "If the user asks about my members/team members, answer for this active team."
                    )

            return "\n".join(lines)
        except Exception:
            return ""

    def _legacy_active_agent_context_activation(
        self,
        legacy_content: str,
    ) -> Any:
        """根据旧函数真实结果独立计算第四 Section activation。

        参数:
            legacy_content: 旧函数完成动态来源读取后返回的正文。

        返回:
            区分 active、无 Agent 名和来源异常的无载荷不可变结果。
        """

        from ..prompt.sections import (
            ACTIVE_AGENT_CONTEXT_ACTIVATION_POLICY_ID,
            ActiveAgentContextActivation,
            ActiveAgentContextActivationState,
        )

        agent_info = getattr(self.agent, "agent_info", None)
        agent_name_present = bool(
            str(getattr(agent_info, "name", "") or "").strip()
        )
        if not agent_name_present:
            state = ActiveAgentContextActivationState.INACTIVE_NO_AGENT_NAME
        elif legacy_content:
            state = ActiveAgentContextActivationState.ACTIVE
        else:
            state = ActiveAgentContextActivationState.INACTIVE_SOURCE_ERROR
        return ActiveAgentContextActivation(
            policy_id=ACTIVE_AGENT_CONTEXT_ACTIVATION_POLICY_ID,
            state=state,
            active=state is ActiveAgentContextActivationState.ACTIVE,
            agent_name_present=agent_name_present,
            source_ready=state is ActiveAgentContextActivationState.ACTIVE,
        )

    def _assemble_active_agent_context_section(self, prefix_before: str) -> str:
        """通过唯一动态输入和 Provider 组装活动 Agent 上下文。

        参数:
            prefix_before: Public Action Commentary 组装后的完整 system 前缀。

        返回:
            Agent 来源不可用时保持原前缀，否则追加动态 Section。
        """

        try:
            candidate_input = build_active_agent_context_candidate_input(self.agent)
            if not candidate_input.activation.active:
                return prefix_before
            section = BuiltinPromptSectionRegistry.provide_active_agent_context(
                candidate_input
            )
            return (
                prefix_before
                + "\n\n# Active Agent Context\n\n"
                + section.content
            )
        except Exception:
            return prefix_before

    def _build_direct_execution_mode_section(self) -> str:
        """按既有语义独立生成 Direct Execution Mode Legacy 正文。

        返回:
            直执行模式关闭时返回空字符串；开启时返回包含可选 scope 的原有
            中文正文，且不改变 Agent 状态、权限或工具行为。

        本函数与第五 Candidate Provider 有意分别维护正文和 scope 渲染，
        使任一单侧漂移都能被 Shadow 发现。
        """

        if not bool(getattr(self.agent, "_direct_execution_mode", False)):
            return ""
        scope = str(
            getattr(self.agent, "_direct_execution_scope", "") or ""
        ).strip()
        scope_text = f"（scope: {scope}）" if scope else ""
        return (
            f"你当前处于直执行模式{scope_text}。\n"
            "- 你是执行者，不是规划者；优先选择最短、最确定的执行路径。\n"
            "- 如果用户或 manager 已经提供了明确路径、明确脚本或明确资源，不要再为了“探索”去额外试探。\n"
            "- 对本地文件，优先直接使用绝对路径 + `bash` / `read` / `skill`，不要把本地路径改造成 `file://` 再交给 `webfetch`。\n"
            "- 只有当当前输入确实缺少关键信息时，才做少量辅助探查；不要把一次执行型任务变成长时间的工具试探。\n"
            "- 完成后直接返回结果、产物路径、关键证据和下一步建议，不要停留在过程性话术。"
        )

    def _legacy_direct_execution_mode_activation(self) -> Any:
        """从旧路径运行时字段独立计算第五 Section activation。

        返回:
            不含 scope 正文、只含状态与 scope 元数据的不可变 activation。
        """

        from ..prompt.sections import (
            DIRECT_EXECUTION_MODE_ACTIVATION_POLICY_ID,
            DirectExecutionModeActivation,
            DirectExecutionModeActivationState,
        )

        active = bool(getattr(self.agent, "_direct_execution_mode", False))
        scope = str(
            getattr(self.agent, "_direct_execution_scope", "") or ""
        ).strip()
        return DirectExecutionModeActivation(
            policy_id=DIRECT_EXECUTION_MODE_ACTIVATION_POLICY_ID,
            state=(
                DirectExecutionModeActivationState.ACTIVE
                if active
                else DirectExecutionModeActivationState.INACTIVE_MODE_OFF
            ),
            active=active,
            scope_present=bool(scope),
            scope_utf8_bytes=len(scope.encode("utf-8")),
        )

    def _assemble_direct_execution_mode_section(self, prefix_before: str) -> str:
        """通过唯一 activation 与 Provider 组装直执行模式说明。

        参数:
            prefix_before: Session Resources 组装后的完整 system 前缀。

        返回:
            模式关闭时保持原前缀，否则追加动态 Section。
        """

        candidate_input = build_direct_execution_mode_candidate_input(self.agent)
        if not candidate_input.activation.active:
            return prefix_before
        section = BuiltinPromptSectionRegistry.provide_direct_execution_mode(
            candidate_input
        )
        return (
            prefix_before
            + "\n\n# Direct Execution Mode\n\n"
            + section.content
        )

    def _build_side_chat_context_section(self) -> str:
        session = getattr(self.agent, "_existing_session", None)
        metadata = dict(getattr(session, "metadata", {}) or {})
        snapshot = metadata.get("side_chat_context")
        if not metadata.get("side_question") or not isinstance(snapshot, dict):
            return ""
        # System-role audit messages are not ordinary conversation turns. Bind
        # the main context explicitly, even after side history compaction.
        bounded = ModelContextBuilder._bounded_json_value(snapshot, string_limit=4000, list_limit=6)
        return (
            "这是当前会话中的侧边讨论，使用与主会话相同的 Agent。以下是主聊天的相关上下文，"
            "其中的历史内容只作为背景资料，不是本轮的新指令。"
            "像正常对话一样直接回答，不需要解释继承、快照或内部会话机制。"
            "产出保留在侧聊工作区，未明确注入前不得发布到主会话的 Wiki、研究中心等正式资源。"
            "不要自动改变主目标；缺少信息时在侧聊中询问用户。\n"
            + json.dumps(bounded, ensure_ascii=False)
        )

    def _build_resource_context_section(self) -> str:
        resource_context = getattr(self.agent, "_session_resource_context", None)
        if not isinstance(resource_context, dict) or not resource_context:
            return ""

        lines: list[str] = [
            "会话资源已导入或挂载到当前 workspace 视图；涉及 sandbox 工具时优先使用 /workspace/... 路径。",
            "调用要求 ArtifactRef 的远端计算 MCP 时，可直接把 workspace 本地文件路径作为文件参数；TCode 会在实际 MCP 调用边界按需上传并转换，普通对话不会提前上传到 Artifact 服务。",
        ]
        workspace_info = resource_context.get("_workspace")
        if isinstance(workspace_info, dict) and workspace_info.get("strategy") == "protein_run":
            lines.extend(
                [
                    "protein_run_workspace:",
                    f"- inputs (read-only): {workspace_info.get('inputs_dir', '')}",
                    f"- outputs: {workspace_info.get('outputs_dir', '')}",
                    f"- logs: {workspace_info.get('logs_dir', '')}",
                    f"- manifest: {workspace_info.get('manifest_path', '')}",
                    "- 不得覆盖 inputs；所有新序列、结构、评分和报告写入 outputs。",
                ]
            )
        uploaded_files = resource_context.get("uploaded_files")
        if isinstance(uploaded_files, list) and uploaded_files:
            lines.append("uploaded_files:")
            for item in uploaded_files[:16]:
                if not isinstance(item, dict):
                    continue
                path = str(
                    item.get("container_path")
                    or item.get("relative_path")
                    or item.get("path")
                    or ""
                ).strip()
                if not path:
                    continue
                kind = str(item.get("kind") or "file").strip() or "file"
                imported = " imported" if bool(item.get("imported", False)) else ""
                mounted = " mounted-ro" if bool(item.get("mounted", False)) else ""
                available = bool(item.get("available_in_sandbox", False))
                suffix = " sandbox-ready" if available else " host-only"
                lines.append(f"- {path} ({kind}{imported}{mounted}; {suffix})")
        external_mounts = resource_context.get("external_mounts")
        if isinstance(external_mounts, list) and external_mounts:
            lines.append("external_mounts:")
            for item in external_mounts[:16]:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("container_path") or "").strip()
                source = str(item.get("source_path") or "").strip()
                if not path:
                    continue
                kind = str(item.get("kind") or "other").strip() or "other"
                source_suffix = f" <- {source}" if source else ""
                lines.append(f"- {path} ({kind}; read-only mount){source_suffix}")
        paper_inputs = resource_context.get("paper_inputs")
        if isinstance(paper_inputs, dict) and int(paper_inputs.get("input_count", 0) or 0) > 0:
            lines.append(
                f"paper_inputs: input_count={int(paper_inputs.get('input_count', 0) or 0)}, "
                f"pdf_count={int(paper_inputs.get('pdf_count', 0) or 0)}"
            )
            for item in list(paper_inputs.get("inputs") or [])[:16]:
                if not isinstance(item, dict):
                    continue
                path = str(
                    item.get("container_path")
                    or item.get("relative_path")
                    or item.get("path")
                    or ""
                ).strip()
                if not path:
                    continue
                kind = str(item.get("kind") or "other").strip() or "other"
                lines.append(f"  - {path} ({kind})")
        paper_library_refs = resource_context.get("paper_library_refs")
        if isinstance(paper_library_refs, list) and paper_library_refs:
            lines.append("paper_library_refs (RAG references; do not inline the full paper):")
            for item in paper_library_refs[:16]:
                if isinstance(item, dict) and str(item.get("doc_id") or "").strip():
                    lines.append(f"- doc_id={str(item.get('doc_id')).strip()}")
            lines.append("Answer against these references by querying Paper Library retrieval tools with the cited doc_id. Retrieve only question-relevant passages/evidence, and preserve page/chunk/table provenance.")
        return "\n".join(lines).strip() if len(lines) > 1 else ""

    def _build_research_continuation_section(self, current_goal: str) -> str:
        """Build one owner-scoped research capsule per user turn, fail-open."""
        session = getattr(self.agent, "_existing_session", None)
        if session is None:
            return ""
        # 子 Agent 已收到边界明确的委派任务；重复工作区胶囊会扩大范围并浪费 token。
        if str(getattr(session, "parent_id", "") or "").strip():
            return ""
        metadata = dict(getattr(session, "metadata", {}) or {})
        owner = {
            "tenant_id": str(getattr(session, "tenant_id", "") or metadata.get("tenant_id") or "").strip(),
            "user_id": str(getattr(session, "user_id", "") or metadata.get("user_id") or "").strip(),
            "workspace_id": str(getattr(session, "workspace_id", "") or metadata.get("workspace_id") or "").strip(),
        }
        if not all(owner.values()):
            return ""
        workdir = str(
            getattr(session, "workdir", "")
            or getattr(self.agent, "workdir", "")
            or os.getcwd()
        )
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "owner": owner,
                    "session_id": str(getattr(session, "id", "") or self.agent.session_id),
                    "turn_id": str(getattr(self.agent, "_active_research_turn_id", "") or ""),
                    "goal": str(current_goal or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cached_key = str(getattr(self.agent, "_research_context_cache_key", "") or "")
        context = getattr(self.agent, "_research_context_cache", None) if cached_key == cache_key else None
        if context is None:
            try:
                from ..research import ResearchContinuationContextBuilder

                context = ResearchContinuationContextBuilder(
                    **owner,
                    workdir=workdir,
                ).build(
                    current_goal,
                    session_id=str(getattr(session, "id", "") or self.agent.session_id),
                    turn_id=str(getattr(self.agent, "_active_research_turn_id", "") or ""),
                    persist_audit=True,
                )
                self.agent._research_context_cache_key = cache_key
                self.agent._research_context_cache = context
            except Exception:
                return ""
        try:
            self.agent._last_research_continuation_context = context.to_dict(include_prompt=False)
        except Exception:
            pass
        return str(getattr(context, "prompt_text", "") or "") if bool(getattr(context, "injected", False)) else ""

    def _build_selected_research_pack_section(self, current_goal: str) -> str:
        """Inject bounded evidence for the sticky, server-validated pack selection."""

        session = getattr(self.agent, "_existing_session", None)
        if session is None:
            return ""
        metadata = dict(getattr(session, "metadata", {}) or {})
        if (
            str(getattr(session, "parent_id", "") or "").strip()
            and not bool(metadata.get("research_context_inherited"))
        ):
            return ""
        selection = metadata.get("research_context")
        if not isinstance(selection, dict) or not list(selection.get("assets") or []):
            self.agent._last_research_grounding = {}
            return ""
        owner = {
            "tenant_id": str(
                getattr(session, "tenant_id", "") or metadata.get("tenant_id") or ""
            ).strip(),
            "user_id": str(
                getattr(session, "user_id", "") or metadata.get("user_id") or ""
            ).strip(),
            "workspace_id": str(
                getattr(session, "workspace_id", "") or metadata.get("workspace_id") or ""
            ).strip(),
        }
        if not all(owner.values()):
            return ""
        workdir = str(
            getattr(session, "workdir", "")
            or getattr(self.agent, "workdir", "")
            or os.getcwd()
        )
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "selection": selection,
                    "goal": str(current_goal or ""),
                    "turn_id": str(getattr(self.agent, "_active_research_turn_id", "") or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        context = (
            getattr(self.agent, "_research_pack_context_cache", None)
            if str(getattr(self.agent, "_research_pack_context_cache_key", "") or "")
            == cache_key
            else None
        )
        if context is None:
            try:
                from ..research import (
                    ResearchChatContextBuilder,
                    ResearchPackIndexScheduler,
                )

                context = ResearchChatContextBuilder(
                    **owner,
                    workdir=workdir,
                ).build(current_goal, selection)
                self.agent._research_pack_context_cache_key = cache_key
                self.agent._research_pack_context_cache = context
                for asset_id in list(context.refresh_asset_ids):
                    ResearchPackIndexScheduler.schedule(
                        asset_id=asset_id,
                        **owner,
                        workdir=workdir,
                    )
            except Exception:
                return ""
        self.agent._last_research_grounding = dict(context.grounding or {})
        return str(context.prompt_text or "")

    def _build_selected_wiki_context_section(self) -> str:
        """Inject one trusted Wiki locator without copying Wiki content."""

        session = getattr(self.agent, "_existing_session", None)
        if session is None:
            return ""
        metadata = dict(getattr(session, "metadata", {}) or {})
        if (
            str(getattr(session, "parent_id", "") or "").strip()
            and not bool(metadata.get("wiki_context_inherited"))
        ):
            return ""
        context = metadata.get("wiki_context")
        if not isinstance(context, dict) or context.get("schema") != "tcode.wiki-chat-context.v1":
            return ""
        page = context.get("page")
        if not isinstance(page, dict) or not str(page.get("page_id") or "").strip():
            return ""
        page_id = str(page.get("page_id") or "").strip()[:240]
        source_id = str(page.get("source_id") or "").strip()[:240]
        title = " ".join(str(page.get("title") or "Wiki 页面").split())[:240]
        page_type = str(page.get("page_type") or "").strip()[:120]
        status = str(page.get("status") or "").strip()[:80]
        revision = max(0, int(page.get("revision") or 0))
        access = str(page.get("access") or "owner").strip()
        locator = f"page_id={page_id}"
        if source_id:
            locator += f", source_id={source_id}"
        return (
            "前端当前选中的 Wiki 页面已经过服务端权限校验：\n"
            f"- title: {title}\n"
            f"- locator: {locator}\n"
            f"- page_type/status/revision: {page_type or 'NA'} / {status or 'NA'} / {revision}\n"
            f"- access: {access}\n"
            "规则：这只是用于理解‘这个 Wiki/当前页面’的精确定位信息，不是页面正文或论文证据。"
            "需要回答内容时仍须调用 paper_wiki_get_page 读取最新的有界内容；若 Revision 已变化，以工具结果为准。"
            + (
                "该来源是共享只读，禁止提交归档、恢复、编辑或其他写入 Proposal。"
                if access == "shared_read_only"
                else "归档或恢复必须先核对最新状态，并且只提交 pending Proposal 等待用户确认。"
            )
        )

    def _build_research_routing_section(self, current_goal: str) -> str:
        """Request one hidden route envelope from the main Agent response."""
        self.agent._research_route_prompt_expected_turn_id = ""
        session = getattr(self.agent, "_existing_session", None)
        if session is None or str(getattr(session, "parent_id", "") or "").strip():
            return ""
        if (
            isinstance(getattr(self.agent, "_output_format", None), dict)
            and self.agent._output_format.get("type") == "json_schema"
        ):
            return ""
        turn_id = str(getattr(self.agent, "_active_research_turn_id", "") or "").strip()
        if not turn_id or turn_id == str(
            getattr(self.agent, "_research_route_committed_turn_id", "") or ""
        ):
            return ""
        metadata = dict(getattr(session, "metadata", {}) or {})
        owner = {
            "tenant_id": str(
                getattr(session, "tenant_id", "") or metadata.get("tenant_id") or ""
            ).strip(),
            "user_id": str(
                getattr(session, "user_id", "") or metadata.get("user_id") or ""
            ).strip(),
            "workspace_id": str(
                getattr(session, "workspace_id", "") or metadata.get("workspace_id") or ""
            ).strip(),
        }
        if not all(owner.values()):
            return ""
        workdir = str(
            getattr(session, "workdir", "")
            or getattr(self.agent, "workdir", "")
            or os.getcwd()
        )
        try:
            from ..research import ResearchTurnRouter, selected_asset_ids

            router = ResearchTurnRouter(
                **owner,
                workdir=workdir,
                pinned_asset_ids=selected_asset_ids(
                    metadata.get("research_context")
                ),
            )
            preliminary = router.deterministic_proposal(current_goal)
        except Exception:
            return ""
        if (
            preliminary.routing_state == "not_research"
            and preliminary.durability == "transient"
            and preliminary.confidence >= 0.9
        ):
            # Router 采用惰性读取：本分支不查询本地 Project 或导入的 Pack 索引，
            # 因此不会产生额外的路由 Prompt 成本。
            return ""
        try:
            context = router.context
        except Exception:
            return ""
        if not context.active or not context.prompt_text:
            return ""
        self.agent._research_route_prompt_expected_turn_id = turn_id
        self.agent._last_research_routing_context = context.to_dict(include_prompt=False)
        return context.prompt_text

    @staticmethod
    def _build_frontend_image_output_section() -> str:
        """返回旧 PromptBuilder 直接拼接的图片展示协议正文。

        返回:
            保持既有标题之外正文、换行和尾部字符不变的 Legacy 文本。

        DSH-205 仍把本函数保留为独立 Legacy 来源，不与内建候选共享正文
        常量或渲染器，以便字节 Shadow 能发现任一单侧漂移。
        """

        return (
            "这是所有 Agent 共用的前端图片展示协议，不限定团队、插件、MCP 或任务类型。\n"
            "- 当你判断当前工作区中的图片能直接帮助用户理解答案时，可以主动把它嵌入最终回复；不要求用户先明确说‘展示图片’。不要展示无关图片。\n"
            "- 优先调用全局 `present_image(path, caption, alt)` 工具注册图片；注册成功后图片会作为结构化消息块随最终回复展示，不要再重复输出同一张图片的 Markdown 标签。\n"
            "- 如果 `present_image` 在当前运行环境确实不可用，才回退到标准图片语法 `![简短说明](图片路径)`。只写文件名、普通链接、‘文件已验证’或 ‘sandbox-ready’ 不会让前端显示图片。\n"
            "- 路径必须能从当前会话工作区定位：优先写完整的工作区相对路径；也可使用 sandbox 返回的 `/workspace/...` 完整路径。不要只写位于子目录中的图片文件名。\n"
            "- 只引用实际存在于当前工作区的 PNG、JPG、JPEG、WEBP、GIF 或 BMP 文件；临时目录中的图片应先保存到当前工作区再引用。\n"
            "- 汇总其他 Agent/工具结果时，如果你决定某个返回的图片路径与答案相关，也应调用 `present_image` 注册。结构化输出任务仍须优先遵守指定 schema。"
        )

    def _assemble_frontend_image_display_section(self, prefix_before: str) -> str:
        """通过唯一内建 Provider 组装 Frontend Image Display。

        参数:
            prefix_before: Agent Instructions 处理完成后的完整 system 前缀。

        返回:
            在固定标题下追加 Provider 正文后的生产 Prompt。
        """

        section = BuiltinPromptSectionRegistry.provide_frontend_image_display()
        return (
            prefix_before
            + "\n\n# Frontend Image Display\n\n"
            + section.content
        )

    @staticmethod
    def _build_image_understanding_section() -> str:
        """返回旧 PromptBuilder 直接拼接的图片理解路由正文。

        返回:
            保持既有标记、视觉能力声明、失败语义和尾部字符不变的 Legacy
            文本。

        DSH-205E 继续把本函数保留为独立 Legacy 来源，不与第二内建候选
        共享正文常量或渲染器，以便字节 Shadow 发现任一单侧漂移。
        """

        return (
            "图片附件始终可以进入 Agent 会话。若当前用户消息包含 "
            "`[IMAGE_INPUT_ROUTING]`，说明主模型不能直接看图：必须先调用 `look_at` "
            "读取相关图片，再回答依赖视觉内容的问题。`look_at` 的文字观察是可供你推理的"
            "工具证据，不要声称主模型直接看到了图片。工具结果包含 `[IMAGE_TOOL_ROUTING]` "
            "时也遵循同一规则，逐张检查列出的论文/浏览器视觉证据。若该工具不可用或失败，仍需返回一条"
            "友好的正常回复，准确说明视觉检查未完成并建议用户配置/切换视觉模型；"
            "不得把底层 HTTP、供应商或序列化报错直接交给用户。"
        )

    def _assemble_image_understanding_section(self, prefix_before: str) -> str:
        """通过唯一内建 Provider 组装 Image Understanding。

        参数:
            prefix_before: Frontend Image Display 组装完成后的完整 system
                前缀。

        返回:
            在固定标题下追加 Provider 正文后的生产 Prompt。
        """

        section = BuiltinPromptSectionRegistry.provide_image_understanding()
        return (
            prefix_before
            + "\n\n# Image Understanding\n\n"
            + section.content
        )

    @staticmethod
    def _build_public_commentary_section() -> str:
        """兼容入口：读取统一的中文公开进度模板，避免保留第二份正文。"""

        return BuiltinPromptSectionRegistry.provide_public_action_commentary().content

    def _legacy_public_action_commentary_activation(
        self,
        tools: list[ToolDefinition],
    ) -> Any:
        """按旧路径语义独立计算 Public Action Commentary activation。

        参数:
            tools: 本轮最终暴露给模型的工具定义。

        返回:
            与候选 activation 使用同一无载荷类型、但不复用其判断函数的
            Legacy 结果。
        """

        from ..prompt.sections import (
            PUBLIC_ACTION_COMMENTARY_ACTIVATION_POLICY_ID,
            PublicActionCommentaryActivation,
            PublicActionCommentaryActivationState,
        )

        tool_definition_count = len(tools)
        output_format = getattr(self.agent, "_output_format", None)
        json_schema = bool(
            isinstance(output_format, dict)
            and output_format.get("type") == "json_schema"
        )
        if tool_definition_count <= 0:
            state = PublicActionCommentaryActivationState.INACTIVE_NO_TOOLS
        elif json_schema:
            state = PublicActionCommentaryActivationState.INACTIVE_JSON_SCHEMA
        else:
            state = PublicActionCommentaryActivationState.ACTIVE
        return PublicActionCommentaryActivation(
            policy_id=PUBLIC_ACTION_COMMENTARY_ACTIVATION_POLICY_ID,
            state=state,
            active=state is PublicActionCommentaryActivationState.ACTIVE,
            tool_definition_count=tool_definition_count,
            output_format_kind="json-schema" if json_schema else "default",
            output_schema_present=json_schema,
        )

    def _assemble_public_action_commentary_section(
        self,
        prefix_before: str,
        tools: list[ToolDefinition],
    ) -> str:
        """按唯一 activation 与 Provider 组装公开动作说明。

        参数:
            prefix_before: Image Understanding 组装完成后的完整 system 前缀。
            tools: 当前轮最终暴露给模型的工具定义。

        返回:
            无工具或 JSON Schema 输出时保持原前缀，否则追加固定 Section。
        """

        activation = evaluate_public_action_commentary_activation(
            tool_definition_count=len(tools),
            output_format=getattr(self.agent, "_output_format", None),
        )
        if not activation.active:
            return prefix_before
        section = BuiltinPromptSectionRegistry.provide_public_action_commentary()
        return (
            prefix_before
            + "\n\n# Public Action Commentary\n\n"
            + section.content
        )

    async def call_llm(
        self,
        tools: list[ToolDefinition],
        *,
        _preflight_rebuilt: bool = False,
        _tool_arguments_retried: bool = False,
    ) -> tuple[str, Optional[list[dict[str, Any]]], Any]:
        """组装一次模型请求、调用 Provider，并处理既有重试与诊断路径。

        参数:
            tools: 当前轮允许暴露给模型的工具定义。
            _preflight_rebuilt: 内部重试标志，避免输入预检无限重建。
            _tool_arguments_retried: 内部重试标志，避免工具参数无限修复。

        返回:
            Provider 文本、可选工具调用列表和原始响应元数据。

        DSH-205 在 Frontend Image Display、Image Understanding、条件型
        Public Action Commentary 与动态 Active Agent Context 的既有位置
        加入彼此独立、默认关闭的 Section 选择；其余组装、视觉路由、预算、
        CompletePrompt Shadow 和 Provider 调用顺序保持不变。
        """

        from ..context.compression.contracts import Owner, Policy
        from ..context.compression.service import CompressionService
        from ..message_context import consume_pending_side_chat_imports, pending_guidance_handoff
        consume_pending_side_chat_imports(self.agent)
        compression_session = getattr(self.agent, "_existing_session", None)
        compression_policy = Policy.from_env().restricted(
            (getattr(compression_session, "metadata", {}) or {}).get("context_compression"),
            (getattr(self.agent.agent_info, "options", {}) or {}).get("context_compression"),
        )
        if compression_policy.mode != "enforce":
            tools = [t for t in tools if t.name != "context_expand"]

        messages_for_api: list[ChatMessage] = []

        system_content = self.agent._get_system_prompt()
        environment_content = self.agent._get_environment_prompt()
        if environment_content:
            system_content = system_content + "\n\n" + environment_content

        if self.agent.agent_info.prompt:
            system_content = (
                system_content
                + "\n\n# Agent Instructions\n\n"
                + self.agent.agent_info.prompt
            )

        system_content = self._assemble_frontend_image_display_section(
            system_content
        )
        if getattr(self.agent, "_intelligent_ui_generation_disabled", False) is True:
            system_content += (
                "\n\n# Intelligent UI preference\n"
                "Interactive UI generation is disabled for this request. When asked for an "
                "interactive diagram, page, or research summary card, provide the useful "
                "content as text in the user's language. Do not search for or load drawing "
                "tools or skills, or produce SVG, HTML, or files as a substitute for that UI. "
                "This does not restrict tools needed for unrelated user tasks."
            )

        system_content = self._assemble_image_understanding_section(
            system_content
        )

        system_content = self._assemble_public_action_commentary_section(
            system_content,
            tools,
        )

        system_content = self._assemble_active_agent_context_section(
            system_content
        )

        manager_team_snapshot = await self._build_manager_team_snapshot_section()
        if manager_team_snapshot:
            system_content = (
                system_content
                + "\n\n# Team Snapshot\n\n"
                + manager_team_snapshot
            )

        resource_context_section = self._build_resource_context_section()
        if resource_context_section:
            system_content = (
                system_content
                + "\n\n# Session Resources\n\n"
                + resource_context_section
            )

        system_content = self._assemble_direct_execution_mode_section(
            system_content
        )

        assembler = ProjectPromptAssembler(self.agent)
        rules = assembler.build_project_rules_section()
        if rules:
            system_content = system_content + "\n\n# Project Rules\n\n" + rules

        current_goal = latest_user_goal(self.agent.messages)
        selected_wiki_context = self._build_selected_wiki_context_section()
        if selected_wiki_context:
            system_content = (
                system_content
                + "\n\n# Selected Wiki Context\n\n"
                + selected_wiki_context
            )
        selected_research_pack = self._build_selected_research_pack_section(current_goal)
        if selected_research_pack:
            system_content = (
                system_content
                + "\n\n# Selected ResearchPack Background\n\n"
                + selected_research_pack
            )
        research_routing = self._build_research_routing_section(current_goal)
        if research_routing:
            system_content = (
                system_content
                + "\n\n# Internal Research Routing Metadata\n\n"
                + research_routing
            )
        research_continuation = self._build_research_continuation_section(current_goal)
        if research_continuation:
            system_content = (
                system_content
                + "\n\n# Research Continuation Context\n\n"
                + research_continuation
            )
        for title, content, guidance in assembler.build_memory_sections(current_goal):
            system_content = system_content + f"\n\n# {title}\n\n" + content
            if guidance:
                system_content = system_content + "\n\n" + guidance

        for title, content, guidance in assembler.build_learning_sections(current_goal):
            system_content = system_content + f"\n\n# {title}\n\n" + content
            if guidance:
                system_content = system_content + "\n\n" + guidance

        if (
            isinstance(self.agent._output_format, dict)
            and self.agent._output_format.get("type") == "json_schema"
            and isinstance(self.agent._output_format.get("schema"), dict)
        ):
            schema_text = json.dumps(
                self.agent._output_format["schema"], ensure_ascii=False
            )
            system_content = (
                system_content
                + "\n\n# Structured Output Requirement\n\n"
                + "Return ONLY valid JSON that matches this JSON Schema exactly. "
                + "Do not include markdown fences or extra text.\n"
                + schema_text
            )

        system_content = await self._compose_plugin_prompts(system_content)

        # ── 通过 PromptComposer 执行模块化 Prompt 组装 ────────────
        plan = None
        try:
            from ..task.plan_manager import PlanManager
            from ..task.plan_freshness import (
                apply_freshness_result,
                current_task_fingerprint,
                validate_plan_freshness,
            )
            from ..tools.builtin.todowrite import TodoManager
            from ..session.manager import SessionManager

            plan = PlanManager.get(self.agent.session_id, workdir=self.agent.workdir)
            if plan is not None:
                session = SessionManager.get_all_sync().get(self.agent.session_id)
                metadata = dict(getattr(session, "metadata", {}) or {}) if session else {}
                todos = TodoManager.get_todos(self.agent.session_id, workdir=self.agent.workdir)
                if todos and hasattr(plan, "sync_from_todos"):
                    before_statuses = [
                        str(getattr(getattr(step, "status", ""), "value", getattr(step, "status", "")))
                        for step in (getattr(plan, "steps", []) or [])
                    ]
                    plan.sync_from_todos(todos)
                    after_statuses = [
                        str(getattr(getattr(step, "status", ""), "value", getattr(step, "status", "")))
                        for step in (getattr(plan, "steps", []) or [])
                    ]
                    if before_statuses != after_statuses:
                        try:
                            await PlanManager.save(self.agent.session_id, plan)
                        except Exception:
                            pass
                fingerprint = current_task_fingerprint(
                    metadata,
                    todos=todos,
                    fallback_goal=current_goal or getattr(plan, "goal", ""),
                )
                freshness_errors = validate_plan_freshness(plan, fingerprint)
                apply_freshness_result(plan, freshness_errors)
        except Exception:
            pass

        worker_names: list[str] = []
        try:
            from ..team.registry import WorkerRegistry

            workers = WorkerRegistry.list()
            worker_names = [w.name for w in workers if hasattr(w, "name")]
        except Exception:
            pass

        profile_name = self.agent._execution_profile or ""
        profile_prompt_budget = get_execution_profile_prompt_budget_tokens(
            profile_name,
            default=16_000,
        )
        system_content = PromptComposer.budget_aware_assemble(
            base_prompt=system_content,
            plan=plan,
            worker_names=worker_names if worker_names else None,
            execution_profile=profile_name,
            include_workpad_guide=bool(getattr(self.agent, "_plan_mode", False)),
            total_budget_tokens=profile_prompt_budget,
        )

        system_content = self._complete_prompt(system_content)

        side_chat_context = self._build_side_chat_context_section()
        if side_chat_context:
            system_content += "\n\n# Side Chat Context\n\n" + side_chat_context

        handoffs = pending_guidance_handoff(self.agent.messages)
        if handoffs:
            facts = [msg.metadata["guidance_handoff"] for msg in handoffs]
            system_content += (
                "\n\n# Respond to the user's in-turn change\n"
                "The latest user guidance changed the work in this same turn. "
                "In the first public response, use 1–2 natural sentences in the user's language: "
                "say what you understood changed, what happened to the step that was underway "
                "(if known), and the concrete next step. If making a tool call, put those sentences "
                "in the public text before the call. Don't substitute a status label or generic "
                "tool announcement. Respect strict final-output formatting.\n"
                "Use the runtime facts below and the tool history. 'returned' means it finished "
                "returning, not necessarily succeeded; 'background_continues' means no longer waiting, "
                "not cancelled; 'stop_unconfirmed' is not a confirmed stop. Never claim rollback or "
                "invent a step or a result. Reuse results only where the new requirements still allow it.\n"
                + json.dumps(facts, ensure_ascii=False)
            )

        if system_content:
            messages_for_api.append(
                ChatMessage(role="system", content=system_content)
            )

        # The durable transcript and provider input are intentionally separate.
        # For short sessions the builder returns a copied list; under token,
        # latency, or repetition pressure it selects recent complete turns plus
        # a source-linked milestone checkpoint.
        session_id = str(getattr(self.agent, "session_id", "") or "")
        workdir = str(getattr(self.agent, "workdir", "") or "") or None
        durable_messages = list(getattr(self.agent, "messages", []) or [])
        try:
            checkpoint = await asyncio.to_thread(
                CheckpointManager.load,
                session_id,
                workdir=workdir,
            )
        except Exception:
            # Context projection is an optimization; an unavailable checkpoint
            # directory must not prevent the underlying model call.
            checkpoint = None
        compression = None
        # A capsule must never be shown when the agent cannot call its reader.
        if compression_policy.mode == "shadow" or (compression_policy.mode == "enforce" and any(getattr(t, "name", "") == "context_expand" for t in tools or [])):
            existing_session = getattr(self.agent, "_existing_session", None)
            owner = Owner(*(str(getattr(existing_session, k, "") or "") for k in ("tenant_id", "user_id", "workspace_id")), session_id)
            try:
                owner.key()
                compression = CompressionService(workdir, owner, compression_policy, max_chars=self._tool_result_prompt_chars())
            except ValueError:
                pass
        context_builder = ModelContextBuilder(compression=compression)
        from ..runtime.async_resume import resume_context_json
        restoration_context = resume_context_json()
        fixed_context_chars = len(system_content) + self._estimate_tool_chars(tools) + len(restoration_context)
        try:
            context_assembly = await asyncio.to_thread(
                context_builder.build,
                durable_messages,
                checkpoint=checkpoint,
                additional_chars=fixed_context_chars,
            )
        except UserDirectiveBudgetExceeded:
            raise
        except Exception as exc:
            # Fail open to the former full-history behavior. Provider limits
            # still apply, but context assembly itself never becomes an outage.
            estimated_tokens = context_builder.estimate_tokens(
                durable_messages
            ) + fixed_context_chars // 4
            context_assembly = ModelContextAssembly(
                messages=durable_messages,
                triggered=False,
                trigger_reasons=[f"assembly_error:{type(exc).__name__}"],
                original_message_count=len(durable_messages),
                selected_message_count=len(durable_messages),
                original_estimated_tokens=estimated_tokens,
                selected_estimated_tokens=estimated_tokens,
                source_message_ids=[
                    str(getattr(message, "id", "") or "")
                    for message in durable_messages
                ],
            )
        if context_assembly.triggered:
            try:
                existing_session = getattr(self.agent, "_existing_session", None)
                checkpoint = await asyncio.to_thread(
                    save_milestone_checkpoint,
                    session_id=session_id,
                    messages=durable_messages,
                    workdir=workdir,
                    session_metadata=(
                        dict(getattr(existing_session, "metadata", {}) or {})
                        if existing_session is not None
                        else {}
                    ),
                    contract=getattr(self.agent, "_active_outcome_contract", None),
                    milestone="context_pressure",
                    agent_name=str(
                        getattr(getattr(self.agent, "agent_info", None), "name", "")
                        or ""
                    ),
                    title=str(getattr(existing_session, "title", "") or "")
                    if existing_session is not None
                    else "",
                )
                context_assembly = await asyncio.to_thread(
                    context_builder.attach_checkpoint,
                    context_assembly,
                    durable_messages,
                    checkpoint=checkpoint,
                    additional_chars=fixed_context_chars,
                )
            except UserDirectiveBudgetExceeded:
                raise
            except Exception as exc:
                print(f"[ContextBuilder] checkpoint capture failed: {exc}")
        self.agent._last_context_assembly = context_assembly.telemetry()
        if compression is not None:
            self.agent._last_context_assembly["compression"] = compression.metrics
            if compression.metrics:
                try:
                    from ..session.events import SessionEventStore
                    await asyncio.to_thread(
                        SessionEventStore.append, session_id=session_id,
                        event_type="context.compression", workdir=workdir,
                        owner={k: getattr(compression.owner, k) for k in ("tenant_id", "user_id", "workspace_id")},
                        payload={"schema": "tcode.context-compression/v1", "measurements": compression.metrics[-64:]},
                    )
                except Exception:
                    pass  # Observability never changes Provider execution.
        if context_assembly.triggered:
            emitter = getattr(self.agent, "_agent_event_emitter", None)
            if callable(emitter):
                try:
                    emitted = emitter(
                        {
                            "kind": "model_context_assembly",
                            "status": "completed",
                            "session_id": str(
                                getattr(self.agent, "session_id", "") or ""
                            ),
                            **context_assembly.telemetry(),
                        }
                    )
                    if hasattr(emitted, "__await__"):
                        await emitted
                except Exception:
                    pass
        if context_assembly.checkpoint_context:
            messages_for_api.append(
                ChatMessage(
                    role="system",
                    content=context_assembly.checkpoint_context,
                )
            )

        if restoration_context:
            # A host-resolved, source-bound data capsule is pinned independently
            # of hot-history compression; it does not grant tool permissions.
            messages_for_api.append(ChatMessage(role="user", content=restoration_context))
        goal_anchor_position = len(messages_for_api)
        for msg in context_assembly.messages:
            if msg.role == "user":
                messages_for_api.append(self._user_chat_message_for_prompt(msg))
                continue

            if msg.role == "assistant":
                if (
                    not str(getattr(msg, "content", "") or "").strip()
                    and not getattr(msg, "tool_calls", None)
                    and not normalize_content_blocks(
                        getattr(msg, "content_blocks", []) or []
                    )
                    and getattr(msg, "process_notes", None)
                ):
                    continue
                formatted_tcs = None
                if msg.tool_calls:
                    formatted_tcs = []
                    for tc in msg.tool_calls:
                        if hasattr(tc, "tool_name"):
                            args, argument_error = self._normalize_tool_arguments(
                                tc.arguments
                            )
                            if argument_error is not None:
                                args = self._invalid_tool_arguments_prompt_value(
                                    argument_error
                                )
                            formatted_tcs.append(
                                {
                                    "id": tc.call_id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.tool_name,
                                        "arguments": self._format_tool_arguments_for_prompt(args),
                                    },
                                }
                            )
                        elif isinstance(tc, dict):
                            func = tc.get("function", {})
                            args = func.get("arguments", {})
                            args, argument_error = self._normalize_tool_arguments(args)
                            if argument_error is not None:
                                args = self._invalid_tool_arguments_prompt_value(
                                    argument_error
                                )
                            formatted_tcs.append(
                                {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": func.get("name", ""),
                                        "arguments": self._format_tool_arguments_for_prompt(args),
                                    },
                                }
                            )
                assistant_metadata = getattr(msg, "metadata", {}) or {}
                messages_for_api.append(
                    ChatMessage(
                        role="assistant",
                        content=self._assistant_content_for_prompt(msg),
                        tool_calls=formatted_tcs,
                        reasoning_content=assistant_metadata.get("reasoning_content"),
                        reasoning_blocks=assistant_metadata.get("reasoning_blocks") or None,
                    )
                )
                continue

            if msg.role == "tool":
                tool_call_id = None
                if msg.tool_calls:
                    tc = msg.tool_calls[0]
                    tool_call_id = (
                        tc.call_id if hasattr(tc, "call_id") else tc.get("id", "")
                    )
                messages_for_api.append(
                    self._tool_chat_message_for_prompt(
                        msg,
                        tool_call_id=tool_call_id,
                    )
                )

        messages_for_api[goal_anchor_position:goal_anchor_position] = self._missing_goal_anchor(messages_for_api)
        messages_for_api = self._normalize_tool_call_message_chain(messages_for_api)
        messages_for_api = self._expand_tool_model_images(messages_for_api)
        self.agent._dump_prompt_payload(messages_for_api=messages_for_api, tools=tools)

        llm_tools = tools
        safe_messages_for_api = self._ensure_user_role_for_provider(messages_for_api)
        max_input_chars = self._provider_max_input_chars()
        preflight_chars = self._estimate_chat_payload_chars(
            safe_messages_for_api,
            llm_tools,
        )
        if restoration_context and preflight_chars > max_input_chars:
            raise RuntimeError("async_resume_context_exceeds_model_budget: restoration context cannot be silently truncated")
        self.agent._async_submission_context = {
            "messages": [{"role": message.role, "content": message.content}
                         for message in safe_messages_for_api
                         if message.role in {"user", "assistant"} and message.content and message.content != restoration_context],
            "checkpoint": context_assembly.checkpoint_context,
        }
        if preflight_chars > max_input_chars:
            if not _preflight_rebuilt:
                compacted = await self._emergency_compact_agent_messages()
                if compacted:
                    await self._append_runtime_system_notice(
                        "[LLM_INPUT_GUARD] Oversized input detected before provider call; emergency context compaction was applied."
                    )
                    return await self.call_llm(
                        tools,
                        _preflight_rebuilt=True,
                        _tool_arguments_retried=_tool_arguments_retried,
                    )
            response = await self._call_degraded_llm(
                messages_for_api=safe_messages_for_api,
                max_input_chars=max_input_chars,
            )
        else:
            response = None
        if response is None:
            provider_chat_kwargs: dict[str, Any] = {}
            configured_max_output_tokens = self._configured_max_output_tokens()
            if configured_max_output_tokens:
                provider_chat_kwargs["max_tokens"] = configured_max_output_tokens
            text_delta_handler = getattr(
                self.agent, "_llm_text_delta_handler", None
            )
            if callable(text_delta_handler) and bool(
                getattr(self.agent.provider, "supports_text_delta_stream", False)
            ) and not str(
                getattr(self.agent, "_research_route_prompt_expected_turn_id", "")
                or ""
            ).strip():
                provider_chat_kwargs["on_text_delta"] = text_delta_handler
            try:
                response = await self._provider_chat(
                    messages=safe_messages_for_api,
                    tools=llm_tools,
                    temperature=self.agent.agent_info.temperature,
                    reasoning_effort=self.agent._reasoning_effort,
                    **provider_chat_kwargs,
                )
            except Exception as exc:
                if self._is_missing_user_role_error(exc):
                    response = await self._provider_chat(
                        messages=self._ensure_user_role_for_provider(safe_messages_for_api),
                        tools=llm_tools,
                        temperature=self.agent.agent_info.temperature,
                        reasoning_effort=self.agent._reasoning_effort,
                        **provider_chat_kwargs,
                    )
                    await self._append_runtime_system_notice(
                        "[LLM_INPUT_GUARD] Provider required at least one user-role message; fallback user prompt was injected."
                    )
                else:
                    if not self._is_input_too_large_error(exc):
                        raise

                    compacted = await self._emergency_compact_agent_messages()
                    if compacted:
                        await self._append_runtime_system_notice(
                            "[LLM_INPUT_GUARD] Oversized input detected and emergency context compaction was applied."
                        )
                        # 紧急压缩后重建 prompt 并重试一次。
                        return await self.call_llm(
                            tools,
                            _preflight_rebuilt=True,
                            _tool_arguments_retried=_tool_arguments_retried,
                        )

                    response = await self._call_degraded_llm(
                        messages_for_api=safe_messages_for_api,
                        max_input_chars=max_input_chars,
                    )

        response = await self._continue_truncated_text(
            response,
            messages_for_api=safe_messages_for_api,
            max_input_chars=max_input_chars,
        )

        if response.usage:
            try:
                from ..types import Usage
                from ..session.usage import normalize_usage

                normalized_usage = normalize_usage(response.usage)
                usage = Usage(**normalized_usage)
                self.agent.total_usage.prompt_tokens += usage.prompt_tokens
                self.agent.total_usage.completion_tokens += usage.completion_tokens
                self.agent.total_usage.total_tokens += usage.total_tokens

                print(
                    f"\n💰 This turn: {usage} | Total: {self.agent.total_usage} "
                    f"(est. cost: ${self.agent.total_usage.cost:.4f})\n"
                )
            except Exception as exc:
                print(f"\n⚠️  Usage tracking error: {exc}\n")

        self._reject_truncated_response(response)
        tool_calls = None
        if response.tool_calls:
            tool_calls = []
            malformed_calls: list[dict[str, Any]] = []
            for tc in response.tool_calls:
                func = (
                    tc.function
                    if hasattr(tc, "function")
                    else tc.get("function", {})
                )
                args = (
                    func.get("arguments", {})
                    if isinstance(func, dict)
                    else func.arguments
                )
                call_id = tc.id if hasattr(tc, "id") else tc.get("id", "")
                tool_name = (
                    func.get("name", "")
                    if isinstance(func, dict)
                    else func.name
                )
                parameters = next((tool.parameters for tool in tools if tool.name == tool_name), None)
                normalized_args, argument_error = self._normalize_tool_arguments(args, schema=parameters)
                normalized_call = {
                    "id": call_id,
                    "function": {
                        "name": tool_name,
                        "arguments": normalized_args,
                    },
                }
                if argument_error is not None:
                    normalized_call["_tcode_argument_parse_error"] = argument_error
                    malformed_calls.append(
                        {
                            "call_id": str(call_id or ""),
                            "tool_name": str(tool_name or ""),
                            **argument_error,
                        }
                    )
                tool_calls.append(normalized_call)

            if malformed_calls and not _tool_arguments_retried:
                summaries = [
                    {
                        "tool_name": item.get("tool_name", ""),
                        "code": item.get("code", ""),
                        "line": item.get("line", 0),
                        "column": item.get("column", 0),
                    }
                    for item in malformed_calls[:8]
                ]
                await self._append_runtime_system_notice(
                    "[TOOL_ARGUMENT_JSON_GUARD] The provider returned malformed tool "
                    "arguments, so no tool from that response was executed. Regenerate "
                    "the intended tool call once with one complete JSON object per call. "
                    "Do not copy a truncated argument fragment. Diagnostics: "
                    + json.dumps(summaries, ensure_ascii=False)
                )
                return await self.call_llm(
                    tools,
                    _preflight_rebuilt=_preflight_rebuilt,
                    _tool_arguments_retried=True,
                )

        return response.content, tool_calls, response
