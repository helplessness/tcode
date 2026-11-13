"""OpenAI provider"""

import asyncio
import os
import json
from typing import Any, AsyncIterator, Optional

import openai

from . import openai_responses as responses_wire
from .base import LLMProvider, ChatMessage, ChatResponse, ToolDefinition
from .opencode_go import opencode_go_headers


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible provider supporting Chat Completions and Responses."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        api_mode: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        super().__init__(api_key or os.environ.get("OPENAI_API_KEY", ""))
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self._model = model
        raw_timeout = timeout_seconds
        if raw_timeout is None:
            raw_timeout = os.environ.get("TCODE_LLM_TIMEOUT", "120")
        try:
            parsed_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            parsed_timeout = 120.0
        self._timeout_seconds = max(5.0, min(parsed_timeout, 3600.0))
        # OpenAI-compatible local/custom gateways commonly do not require a
        # credential. The SDK itself rejects an empty key before any request,
        # so use a non-secret transport placeholder only when a custom base URL
        # is explicitly configured. Official OpenAI still requires real auth.
        client_api_key = self._api_key or ("not-required" if self._base_url else None)
        self._client = openai.AsyncOpenAI(
            api_key=client_api_key,
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            max_retries=1,
            default_headers=opencode_go_headers(self._base_url, session_id),
        )
        self._image_inputs_supported: Optional[bool] = None
        requested_mode = (
            responses_wire.normalize_api_mode(api_mode)
            or responses_wire.normalize_api_mode(
                os.environ.get("TCODE_OPENAI_API_MODE")
            )
            or responses_wire.API_MODE_CHAT
        )
        self._api_mode = requested_mode
        # ``auto`` resolves lazily: it prefers Responses until an endpoint-missing
        # error proves the server only implements Chat Completions.
        self._auto_api_mode: str = ""

    @property
    def name(self) -> str:
        return "openai"

    def _provider_error(self, error: Exception, model: Optional[str] = None) -> Exception:
        status = getattr(error, "status_code", None)
        request_id = str(getattr(error, "request_id", "") or "").strip()
        provider_id = str(getattr(self, "provider_id", "") or "openai-compatible")
        detail = f"provider={provider_id}, model={model or self._model or 'default'}"
        if status is not None:
            detail += f", status={status}"
        if request_id:
            detail += f", request_id={request_id[:128]}"
        # Classify known failures without copying arbitrary vendor text, which
        # may echo private prompts, credentials or endpoint addresses.
        body = getattr(error, 'body', None)
        if isinstance(body, dict) and isinstance(body.get('error'), dict):
            body = body['error']
        message = str(body.get('message') or '').lower() if isinstance(body, dict) else ''
        hint = ''
        if 'temperature' in message and any(word in message for word in ('invalid', 'unsupported', 'only 1')):
            hint = '模型拒绝 temperature 参数，请更新模型兼容配置后重试'
        elif 'reasoning_content' in message:
            hint = '模型拒绝了历史消息的 reasoning_content 格式，请核对推理模式与历史消息兼容性'
        elif 'context' in message and any(word in message for word in ('length', 'limit', 'exceed')):
            hint = '请求超过模型上下文限制，请压缩上下文后重试'
        elif 'tool' in message and any(word in message for word in ('schema', 'parameter')):
            hint = '模型拒绝了工具参数定义，请检查工具 Schema 兼容性'
        return Exception(f"Model provider request failed ({detail})" + (f"：{hint}" if hint else ''))

    @staticmethod
    def _normalize_sampling(payload: dict[str, Any], model: str) -> None:
        # Kimi K2.6 rejects the zero-temperature overrides used by auxiliary
        # formatting/judging calls. Its endpoint supplies the supported default.
        if model.lower().rsplit('/', 1)[-1].startswith('kimi-k2.6'):
            payload.pop('temperature', None)

    @property
    def api_mode(self) -> str:
        """The configured mode, or the resolved mode once ``auto`` has decided."""

        mode = self._configured_api_mode()
        if mode == responses_wire.API_MODE_AUTO:
            return self._resolved_auto_mode() or responses_wire.API_MODE_AUTO
        return mode

    def _configured_api_mode(self) -> str:
        return getattr(self, "_api_mode", "") or responses_wire.API_MODE_CHAT

    def _resolved_auto_mode(self) -> str:
        return getattr(self, "_auto_api_mode", "") or ""

    def _use_responses_api(self) -> bool:
        mode = self._configured_api_mode()
        if mode == responses_wire.API_MODE_RESPONSES:
            return True
        if mode == responses_wire.API_MODE_AUTO:
            return self._resolved_auto_mode() != responses_wire.API_MODE_CHAT
        return False

    def _supports_reasoning_effort(self, model_name: str) -> bool:
        normalized = str(model_name or "").strip().lower()
        if not normalized:
            return False
        # DeepSeek V4 supports this OpenAI-compatible field. Dropping an
        # explicit low effort silently selects the provider's default high.
        model_id = normalized.rsplit("/", 1)[-1]
        return (
            normalized.startswith(("gpt-5", "o1", "o3", "o4"))
            or model_id.startswith("deepseek-v4-")
        )

    @staticmethod
    def _estimate_message_chars(openai_messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in openai_messages:
            total += len(str(msg.get("role", "") or ""))
            content = msg.get("content", "") or ""
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        total += len(str(part))
                    elif part.get("type") == "text":
                        total += len(str(part.get("text", "") or ""))
                    elif part.get("type") == "image_url":
                        # Image token accounting is model-specific; avoid counting
                        # base64 transport bytes as prompt text.
                        total += 1024
            else:
                total += len(str(content))
            if msg.get("tool_call_id"):
                total += len(str(msg.get("tool_call_id", "") or ""))
            tool_calls = msg.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        total += len(str(tc))
                        continue
                    total += len(str(tc.get("id", "") or ""))
                    func = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    total += len(str(func.get("name", "") or ""))
                    total += len(str(func.get("arguments", "") or ""))
        return total

    @staticmethod
    def _estimate_tool_chars(openai_tools: Optional[list[dict[str, Any]]]) -> int:
        if not openai_tools:
            return 0
        total = 0
        for tool in openai_tools:
            if not isinstance(tool, dict):
                total += len(str(tool))
                continue
            func = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            total += len(str(func.get("name", "") or ""))
            total += len(str(func.get("description", "") or ""))
            parameters = func.get("parameters", {})
            try:
                total += len(json.dumps(parameters, ensure_ascii=False))
            except Exception:
                total += len(str(parameters))
        return total

    @staticmethod
    def _has_image_parts(messages: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in message.get("content", [])
            )
            for message in messages
        )

    @staticmethod
    def _without_image_parts(
        request_kwargs: dict[str, Any],
        *,
        required_image_inputs: bool = False,
    ) -> dict[str, Any]:
        """Return a provider-compatible copy that keeps the structured text context."""
        fallback = dict(request_kwargs)
        fallback_messages: list[dict[str, Any]] = []
        for raw_message in list(request_kwargs.get("messages") or []):
            message = dict(raw_message)
            content = message.get("content")
            if isinstance(content, list):
                text_parts = [
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                had_image = any(
                    isinstance(part, dict) and part.get("type") == "image_url"
                    for part in content
                )
                text_content = "\n".join(
                    item for item in text_parts if item.strip()
                ).strip()
                if had_image:
                    if required_image_inputs:
                        note = (
                            "[IMAGE_INPUT_ROUTING: Direct image input was rejected by "
                            "this model endpoint. The uploaded image was not discarded. "
                            "Use the look_at tool with the workspace path shown in the "
                            "user message before answering questions that depend on image "
                            "content. If visual inspection is unavailable, respond with a "
                            "friendly capability diagnosis; do not expose the provider error.]"
                        )
                    else:
                        note = (
                            "[Browser observation image omitted because this model endpoint "
                            "does not accept image input. Rely on the structured browser "
                            "observation and current page_revision above.]"
                        )
                    text_content = f"{text_content}\n\n{note}".strip()
                message["content"] = text_content
            fallback_messages.append(message)
        fallback["messages"] = fallback_messages
        return fallback

    @staticmethod
    def _is_image_input_unsupported(exc: Exception) -> bool:
        detail = str(exc or "").lower()
        signals = (
            "image_url",
            "image input",
            "image content",
            "multimodal",
            "unknown variant `image",
            'unknown variant "image',
        )
        return any(signal in detail for signal in signals)

    @staticmethod
    def _has_required_image_inputs(messages: list[ChatMessage]) -> bool:
        return any(
            str(image.get("source") or "") in {
                "user_upload", "paper_visual", "computer_observation"
            }
            for message in messages
            for image in list(message.images or [])
            if isinstance(image, dict)
        )

    async def _create_completion_with_image_fallback(
        self,
        request_kwargs: dict[str, Any],
        *,
        allow_fallback: bool = True,
        required_image_inputs: bool = False,
    ) -> Any:
        # Required images (user uploads, paper visuals, computer observations)
        # must never be silently removed for a second text-only request.
        allow_fallback = bool(allow_fallback and not required_image_inputs)
        # 原生工具调用不由 SDK 静默重发；工具纠错仍使用原有有界规则。
        native_tools = bool(request_kwargs.get("tools"))
        if native_tools:
            allow_fallback = False
        client = self._client.with_options(max_retries=0) if native_tools else self._client
        messages = list(request_kwargs.get("messages") or [])
        has_images = self._has_image_parts(messages)
        effective = (
            self._without_image_parts(
                request_kwargs,
                required_image_inputs=required_image_inputs,
            )
            if has_images and self._image_inputs_supported is False and allow_fallback
            else request_kwargs
        )
        try:
            response = await client.chat.completions.create(**effective)
            if has_images and effective is request_kwargs:
                self._image_inputs_supported = True
            return response
        except openai.BadRequestError as exc:
            if (
                not has_images
                or effective is not request_kwargs
                or not self._is_image_input_unsupported(exc)
            ):
                raise
            if not allow_fallback:
                raise ValueError(
                    "当前模型或接口拒绝了图片输入；请选择支持视觉输入的模型后重试。"
                ) from exc
            self._image_inputs_supported = False
            return await self._client.chat.completions.create(
                **self._without_image_parts(
                    request_kwargs,
                    required_image_inputs=required_image_inputs,
                )
            )

    @staticmethod
    def _max_input_chars() -> int:
        raw = str(os.environ.get("TCODE_OPENAI_MAX_INPUT_CHARS", "250000") or "250000").strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 250000
        return max(10000, value)

    async def close(self):
        close_fn = getattr(self._client, "close", None)
        if close_fn:
            result = close_fn()
            if hasattr(result, "__await__"):
                await result

    @property
    def supports_text_delta_stream(self) -> bool:
        """Whether ``chat`` can project public text deltas while it runs."""
        return True

    @property
    def stream_cancel_mode(self) -> str:
        return "connection_close"

    @staticmethod
    def _to_openai_payload(
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]],
    ) -> tuple[list[dict[str, Any]], Optional[list[dict[str, Any]]]]:
        openai_messages: list[dict[str, Any]] = []
        for msg in messages:
            content: Any = msg.content
            if msg.images and msg.role in {"user", "assistant"}:
                content = []
                if str(msg.content or "").strip():
                    content.append({"type": "text", "text": msg.content})
                for image in msg.images[:2]:
                    data_url = str(image.get("data_url") or "").strip()
                    if not data_url:
                        continue
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": data_url,
                                "detail": str(image.get("detail") or "low"),
                            },
                        }
                    )
            if msg.role == "system":
                openai_messages.append({"role": "system", "content": msg.content})
            elif msg.role == "tool":
                openai_messages.append(
                    {
                        "role": "tool",
                        "content": msg.content,
                        "tool_call_id": msg.tool_call_id,
                    }
                )
            elif msg.role == "assistant" and msg.tool_calls:
                openai_messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        **({"reasoning_content": msg.reasoning_content} if msg.reasoning_content is not None else {}),
                        "tool_calls": [
                            {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": tc.get("function", {}).get("name", ""),
                                    "arguments": tc.get("function", {}).get(
                                        "arguments", ""
                                    ),
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
            else:
                openai_messages.append(
                    {"role": msg.role, "content": content,
                     **({"reasoning_content": msg.reasoning_content} if msg.role == "assistant" and msg.reasoning_content is not None else {})}
                )

        openai_tools = None
        if tools:
            openai_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        return openai_messages, openai_tools

    def _request_kwargs(
        self,
        *,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        openai_messages, openai_tools = self._to_openai_payload(messages, tools)
        target_model = model or self._model or "gpt-4o"
        reasoning_effort = extra.pop("reasoning_effort", None)
        if reasoning_effort and not self._supports_reasoning_effort(target_model):
            reasoning_effort = None

        input_chars = self._estimate_message_chars(
            openai_messages
        ) + self._estimate_tool_chars(openai_tools)
        max_input_chars = self._max_input_chars()
        if input_chars > max_input_chars:
            raise Exception(
                "LLM input too large before request: "
                f"{input_chars} chars exceeds configured limit {max_input_chars} chars "
                f"(base_url={self._base_url}, model={target_model}). "
                "Reduce context or enable stronger compaction."
            )

        request_kwargs: dict[str, Any] = {
            "model": target_model,
            "messages": openai_messages,
            "timeout": self._timeout_seconds,
        }
        if openai_tools:
            request_kwargs["tools"] = openai_tools
        if temperature is not None:
            request_kwargs["temperature"] = float(temperature)
        if max_tokens is not None:
            request_kwargs["max_tokens"] = int(max_tokens)
        if reasoning_effort is not None:
            request_kwargs["reasoning_effort"] = reasoning_effort
        request_kwargs.update(
            {key: value for key, value in extra.items() if value is not None}
        )
        self._normalize_sampling(request_kwargs, target_model)
        if target_model.lower().rsplit("/", 1)[-1].startswith("deepseek-v4-") and openai_tools:
            # Legacy history predating reasoning-content persistence cannot be
            # replayed in DeepSeek thinking mode with tools.
            missing_reasoning = any(
                message.get("role") == "assistant" and "reasoning_content" not in message
                for message in openai_messages
            )
            if missing_reasoning:
                request_kwargs["extra_body"] = {
                    **dict(request_kwargs.get("extra_body") or {}),
                    "thinking": {"type": "disabled"},
                }
        return request_kwargs

    # ------------------------------------------------------------------
    # Protocol dispatch: Chat Completions vs Responses
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        call_mode = responses_wire.normalize_api_mode(kwargs.pop('api_mode', None))
        if call_mode == responses_wire.API_MODE_CHAT or not self._use_responses_api():
            return await self._chat_completions_chat(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        try:
            response = await self._responses_chat(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (
                self._configured_api_mode() == responses_wire.API_MODE_AUTO
                and responses_wire.is_endpoint_unsupported(exc)
            ):
                self._auto_api_mode = responses_wire.API_MODE_CHAT
                return await self._chat_completions_chat(
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            raise
        if self._configured_api_mode() == responses_wire.API_MODE_AUTO:
            self._auto_api_mode = responses_wire.API_MODE_RESPONSES
        return response

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatResponse]:
        if not self._use_responses_api():
            async for part in self._chat_completions_chat_stream(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ):
                yield part
            return

        emitted = False
        try:
            async for part in self._responses_chat_stream(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            ):
                emitted = True
                yield part
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only fall back before any delta reached the caller; a half-emitted
            # stream cannot be replayed against a different protocol.
            if (
                not emitted
                and self._configured_api_mode() == responses_wire.API_MODE_AUTO
                and responses_wire.is_endpoint_unsupported(exc)
            ):
                self._auto_api_mode = responses_wire.API_MODE_CHAT
                async for part in self._chat_completions_chat_stream(
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                ):
                    yield part
                return
            raise
        if self._configured_api_mode() == responses_wire.API_MODE_AUTO:
            self._auto_api_mode = responses_wire.API_MODE_RESPONSES

    async def _accumulate_stream(
        self,
        stream: AsyncIterator[ChatResponse],
        on_text_delta: Any,
    ) -> ChatResponse:
        """Fold streamed parts into one response while projecting text deltas."""

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        finish_reason = None
        usage = None
        reported_model = None
        async for part in stream:
            reported_model = part.reported_model or reported_model
            if part.content:
                content_parts.append(part.content)
                try:
                    emitted = on_text_delta(part.content)
                    if hasattr(emitted, "__await__"):
                        await emitted
                except Exception:
                    # UI progress delivery must not fail the model request.
                    pass
            if part.reasoning_content:
                reasoning_parts.append(part.reasoning_content)
            for tc in part.tool_calls or []:
                index = int(tc.get("index", 0) or 0)
                current = tool_call_parts.setdefault(
                    index,
                    {
                        "id": "",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                current["id"] += str(tc.get("id", "") or "")
                function = tc.get("function", {}) or {}
                current["function"]["name"] += str(function.get("name", "") or "")
                current["function"]["arguments"] += str(
                    function.get("arguments", "") or ""
                )
            if part.finish_reason:
                finish_reason = part.finish_reason
            if part.usage:
                usage = part.usage

        return ChatResponse(
            content="".join(content_parts),
            reported_model=reported_model,
            reasoning_content="".join(reasoning_parts) or None,
            tool_calls=[tool_call_parts[index] for index in sorted(tool_call_parts)]
            or None,
            finish_reason=finish_reason,
            usage=usage,
        )

    # ------------------------------------------------------------------
    # Responses API path
    # ------------------------------------------------------------------

    def _responses_request_payload(
        self,
        *,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        target_model = model or self._model or "gpt-4o"
        reasoning_effort = extra.pop("reasoning_effort", None)
        payload = responses_wire.build_responses_payload(
            messages,
            tools,
            model=target_model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            supports_reasoning_effort=self._supports_reasoning_effort(target_model),
            extra=extra,
        )
        self._normalize_sampling(payload, target_model)
        input_chars = responses_wire.estimate_payload_chars(payload)
        max_input_chars = self._max_input_chars()
        if input_chars > max_input_chars:
            raise Exception(
                "LLM input too large before request: "
                f"{input_chars} chars exceeds configured limit {max_input_chars} chars "
                f"(base_url={self._base_url}, model={target_model}). "
                "Reduce context or enable stronger compaction."
            )
        return payload

    async def _responses_create_with_image_fallback(
        self,
        payload: dict[str, Any],
        *,
        allow_fallback: bool = True,
        required_image_inputs: bool = False,
    ) -> Any:
        allow_fallback = bool(allow_fallback and not required_image_inputs)
        has_images = responses_wire.payload_has_image_inputs(payload)
        effective = (
            responses_wire.without_image_inputs(
                payload,
                required_image_inputs=required_image_inputs,
            )
            if has_images and self._image_inputs_supported is False and allow_fallback
            else payload
        )
        try:
            response = await self._client.responses.create(**effective)
            if has_images and effective is payload:
                self._image_inputs_supported = True
            return response
        except openai.BadRequestError as exc:
            if (
                not has_images
                or effective is not payload
                or not responses_wire.is_image_input_unsupported(exc)
            ):
                raise
            if not allow_fallback:
                raise ValueError(
                    "当前模型或接口拒绝了图片输入；请选择支持视觉输入的模型后重试。"
                ) from exc
            self._image_inputs_supported = False
            return await self._client.responses.create(
                **responses_wire.without_image_inputs(
                    payload,
                    required_image_inputs=required_image_inputs,
                )
            )

    async def _responses_chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        try:
            on_text_delta = kwargs.pop("on_text_delta", None)
            if callable(on_text_delta):
                return await self._accumulate_stream(
                    self._responses_chat_stream(
                        messages=messages,
                        tools=tools,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **kwargs,
                    ),
                    on_text_delta,
                )

            payload = self._responses_request_payload(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=dict(kwargs),
            )
            required_image_inputs = self._has_required_image_inputs(messages)
            response = await self._responses_create_with_image_fallback(
                payload,
                allow_fallback=True,
                required_image_inputs=required_image_inputs,
            )
            return responses_wire.response_to_chat_response(response)
        except asyncio.CancelledError:
            raise
        except openai.APITimeoutError as e:
            raise Exception(
                f"LLM request timed out after {self._timeout_seconds}s (base_url={self._base_url}, model={model or self._model}): {e}"
            )
        except openai.APIConnectionError as e:
            raise Exception(
                f"LLM connection error (base_url={self._base_url}, model={model or self._model}): {e}"
            )
        except openai.APIStatusError as e:
            # Preserve the typed error so ``auto`` can detect a missing endpoint.
            if responses_wire.is_endpoint_unsupported(e):
                raise
            raise self._provider_error(e, model)
        except openai.APIError as e:
            raise self._provider_error(e, model)

    async def _responses_chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatResponse]:
        stream = None
        try:
            payload = self._responses_request_payload(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=dict(kwargs),
            )
            payload["stream"] = True
            required_image_inputs = self._has_required_image_inputs(messages)
            stream = await self._responses_create_with_image_fallback(
                payload,
                allow_fallback=True,
                required_image_inputs=required_image_inputs,
            )
            state = responses_wire.ResponsesStreamState()
            async for event in stream:
                for part in state.feed(event):
                    yield part
            if state.failed_error:
                raise responses_wire.ResponsesApiError(state.failed_error)
        except asyncio.CancelledError:
            raise
        except responses_wire.ResponsesApiError:
            raise
        except openai.APITimeoutError as e:
            raise Exception(
                f"LLM request timed out after {self._timeout_seconds}s "
                f"(base_url={self._base_url}, model={model or self._model}): {e}"
            )
        except openai.APIConnectionError as e:
            raise Exception(
                f"LLM connection error (base_url={self._base_url}, "
                f"model={model or self._model}): {e}"
            )
        except openai.APIStatusError as e:
            # Preserve the typed error so ``auto`` can detect a missing endpoint.
            if responses_wire.is_endpoint_unsupported(e):
                raise
            raise self._provider_error(e, model)
        except openai.APIError as e:
            raise self._provider_error(e, model)
        finally:
            close_fn = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if callable(close_fn):
                closed = close_fn()
                if hasattr(closed, "__await__"):
                    await closed

    async def _chat_completions_chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        try:
            on_text_delta = kwargs.pop("on_text_delta", None)
            if callable(on_text_delta):
                return await self._accumulate_stream(
                    self._chat_completions_chat_stream(
                        messages=messages,
                        tools=tools,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **kwargs,
                    ),
                    on_text_delta,
                )

            request_kwargs = self._request_kwargs(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=dict(kwargs),
            )

            required_image_inputs = self._has_required_image_inputs(messages)
            response = await self._create_completion_with_image_fallback(
                request_kwargs,
                allow_fallback=True,
                required_image_inputs=required_image_inputs,
            )

            choice = response.choices[0]
            message = choice.message

            return ChatResponse(
                content=message.content or "",
                reported_model=getattr(response, "model", None),
                reasoning_content=getattr(message, "reasoning_content", None),
                tool_calls=[
                    {
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
                if message.tool_calls
                else None,
                finish_reason=choice.finish_reason,
                usage=(
                    {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                    if response.usage
                    else None
                ),
            )
        except asyncio.CancelledError:
            raise
        except openai.APITimeoutError as e:
            raise Exception(
                f"LLM request timed out after {self._timeout_seconds}s (base_url={self._base_url}, model={model or self._model}): {e}"
            )
        except openai.APIConnectionError as e:
            raise Exception(
                f"LLM connection error (base_url={self._base_url}, model={model or self._model}): {e}"
            )
        except openai.APIError as e:
            raise self._provider_error(e, model)

    async def _chat_completions_chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatResponse]:
        stream = None
        try:
            request_kwargs = self._request_kwargs(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=dict(kwargs),
            )
            request_kwargs["stream"] = True
            request_kwargs.setdefault("stream_options", {"include_usage": True})
            try:
                required_image_inputs = self._has_required_image_inputs(messages)
                stream = await self._create_completion_with_image_fallback(
                    request_kwargs,
                    allow_fallback=True,
                    required_image_inputs=required_image_inputs,
                )
            except openai.BadRequestError as exc:
                # Some OpenAI-compatible gateways stream correctly but do not
                # implement the optional usage trailer yet.
                if request_kwargs.get("tools") or "stream_options" not in str(exc).lower():
                    raise
                request_kwargs.pop("stream_options", None)
                stream = await self._create_completion_with_image_fallback(
                    request_kwargs,
                    allow_fallback=True,
                    required_image_inputs=required_image_inputs,
                )

            async for chunk in stream:
                usage = None
                if getattr(chunk, "usage", None) is not None:
                    usage = {
                        "prompt_tokens": int(
                            getattr(chunk.usage, "prompt_tokens", 0) or 0
                        ),
                        "completion_tokens": int(
                            getattr(chunk.usage, "completion_tokens", 0) or 0
                        ),
                        "total_tokens": int(
                            getattr(chunk.usage, "total_tokens", 0) or 0
                        ),
                    }

                choices = list(getattr(chunk, "choices", None) or [])
                if not choices:
                    if usage:
                        yield ChatResponse(content="", usage=usage, reported_model=getattr(chunk, "model", None))
                    continue

                choice = choices[0]
                delta = getattr(choice, "delta", None)
                content = str(getattr(delta, "content", "") or "")
                tool_call_deltas = []
                for tc in list(getattr(delta, "tool_calls", None) or []):
                    function = getattr(tc, "function", None)
                    tool_call_deltas.append(
                        {
                            "index": int(getattr(tc, "index", 0) or 0),
                            "id": str(getattr(tc, "id", "") or ""),
                            "function": {
                                "name": str(getattr(function, "name", "") or ""),
                                "arguments": str(
                                    getattr(function, "arguments", "") or ""
                                ),
                            },
                        }
                    )
                yield ChatResponse(
                    content=content,
                    reported_model=getattr(chunk, "model", None),
                    reasoning_content=str(getattr(delta, "reasoning_content", "") or "") or None,
                    tool_calls=tool_call_deltas or None,
                    finish_reason=getattr(choice, "finish_reason", None),
                    usage=usage,
                )
        except asyncio.CancelledError:
            raise
        except openai.APITimeoutError as e:
            raise Exception(
                f"LLM request timed out after {self._timeout_seconds}s "
                f"(base_url={self._base_url}, model={model or self._model}): {e}"
            )
        except openai.APIConnectionError as e:
            raise Exception(
                f"LLM connection error (base_url={self._base_url}, "
                f"model={model or self._model}): {e}"
            )
        except openai.APIError as e:
            raise self._provider_error(e, model)
        finally:
            # OpenAI-compatible stream implementations are not perfectly
            # uniform, so support both close spellings. This runs when a newer
            # guidance item supersedes and cancels the model task as well.
            close_fn = getattr(stream, "close", None) or getattr(stream, "aclose", None)
            if callable(close_fn):
                closed = close_fn()
                if hasattr(closed, "__await__"):
                    await closed
