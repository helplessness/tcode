"""Anthropic Claude provider"""

import asyncio
import inspect
import os
from contextlib import suppress
from typing import Any, AsyncIterator, Optional

import anthropic

from .base import LLMProvider, ChatMessage, ChatResponse, ToolDefinition


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ):
        super().__init__(api_key or os.environ.get("ANTHROPIC_API_KEY", ""))
        # Allow pointing the SDK at a custom Anthropic-compatible gateway
        # (local proxies, self-hosted relays). Without this the client always
        # targets api.anthropic.com.
        resolved_base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
        client_kwargs: dict[str, Any] = {
            "api_key": self._api_key or "local-anthropic"
        }
        if resolved_base_url:
            client_kwargs["base_url"] = resolved_base_url
        if timeout_seconds:
            try:
                client_kwargs["timeout"] = float(timeout_seconds)
            except (TypeError, ValueError):
                pass
        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._model = model
        self._base_url = resolved_base_url
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def supports_text_delta_stream(self) -> bool:
        return True

    @property
    def stream_cancel_mode(self) -> str:
        # The SDK stream context manager closes its HTTP response on exit.
        return "connection_close"

    def _supported_message_params(self) -> set[str]:
        """Message-create params accepted by the installed SDK version.

        Anthropic removed ``temperature`` from ``messages.create`` in newer SDK
        releases. Introspecting the signature lets one adapter serve several SDK
        versions instead of hard-coding today's keyword set.
        """

        cached = getattr(self, "_message_param_names", None)
        if cached is not None:
            return cached
        try:
            names = set(inspect.signature(self._client.messages.create).parameters)
        except (TypeError, ValueError):
            names = set()
        self._message_param_names = names
        return names

    @staticmethod
    def _image_blocks(images: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for image in list(images or [])[:2]:
            if not isinstance(image, dict):
                continue
            data_url = str(image.get("data_url") or "").strip()
            if not data_url.startswith("data:") or ";base64," not in data_url:
                continue
            header, encoded = data_url.split(",", 1)
            media_type = header[5:].split(";", 1)[0]
            if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": encoded,
                    },
                }
            )
        return blocks

    @classmethod
    def _multimodal_content(cls, message: ChatMessage) -> Any:
        if not message.images:
            return message.content
        parts: list[dict[str, Any]] = []
        if str(message.content or "").strip():
            parts.append({"type": "text", "text": message.content})
        parts.extend(cls._image_blocks(message.images))
        return parts or message.content

    @staticmethod
    def _anthropic_reasoning_blocks(
        blocks: Optional[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Keep only replayable Anthropic thinking blocks from an opaque list.

        Blocks produced by other providers (for example OpenAI Responses
        reasoning items) are ignored, so a mixed-model history never leaks a
        foreign shape into the Anthropic payload.
        """

        result: list[dict[str, Any]] = []
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "thinking":
                signature = str(block.get("signature") or "")
                if not signature:
                    # An unsigned thinking block cannot be replayed.
                    continue
                result.append(
                    {
                        "type": "thinking",
                        "thinking": str(block.get("thinking") or ""),
                        "signature": signature,
                    }
                )
            elif block_type == "redacted_thinking":
                data = str(block.get("data") or "")
                if data:
                    result.append({"type": "redacted_thinking", "data": data})
        return result

    def _assistant_content_blocks(
        self,
        message: ChatMessage,
        reasoning_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Rebuild assistant blocks in Anthropic order: thinking -> text -> tool_use."""

        blocks: list[dict[str, Any]] = list(reasoning_blocks)
        if str(message.content or "").strip():
            blocks.append({"type": "text", "text": message.content})
        for tool_call in message.tool_calls or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function", {}) or {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_call.get("id", ""),
                    "name": function.get("name", ""),
                    "input": function.get("arguments", {}),
                }
            )
        return blocks

    @staticmethod
    def _normalize_finish_reason(raw: Any) -> Optional[str]:
        """Map Anthropic stop reasons onto the canonical finish vocabulary."""

        reason = str(raw or "").strip()
        if not reason:
            return None
        return {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
        }.get(reason, reason)

    @staticmethod
    def _thinking_config(
        reasoning_effort: Any,
        max_tokens_value: Optional[int],
    ) -> Optional[dict[str, Any]]:
        """Map a generic reasoning effort onto Anthropic extended thinking."""

        effort = str(reasoning_effort or "").strip().lower()
        if effort == "none":
            return {"type": "disabled"}
        budget_map = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 16384}
        if effort not in budget_map:
            return None
        budget = budget_map[effort]
        if max_tokens_value:
            # The thinking budget must stay strictly below max_tokens and leave
            # room for the visible answer.
            budget = min(budget, int(max_tokens_value) - 1024)
        if budget < 1024:
            return None
        return {"type": "enabled", "budget_tokens": budget}

    async def close(self):
        close_fn = getattr(self._client, "close", None)
        if close_fn:
            result = close_fn()
            if hasattr(result, "__await__"):
                await result

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        # Convert messages to Anthropic format
        anthropic_messages = []
        system_prompt = ""

        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
                continue

            if msg.role == "tool":
                tool_result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }
                image_blocks = self._image_blocks(msg.images)
                if image_blocks:
                    tool_result["content"] = (
                        [{"type": "text", "text": msg.content}]
                        if str(msg.content or "").strip()
                        else []
                    ) + image_blocks
                if msg.is_error:
                    tool_result["is_error"] = True
                # Parallel tool calls must receive all results in the same
                # immediately following user message.
                previous = anthropic_messages[-1] if anthropic_messages else None
                if (
                    previous
                    and previous["role"] == "user"
                    and isinstance(previous["content"], list)
                    and previous["content"]
                    and all(block.get("type") == "tool_result" for block in previous["content"])
                ):
                    previous["content"].append(tool_result)
                else:
                    anthropic_messages.append(
                        {"role": "user", "content": [tool_result]}
                    )
                continue

            if msg.role == "assistant":
                reasoning_blocks = self._anthropic_reasoning_blocks(
                    msg.reasoning_blocks
                )
                if reasoning_blocks or msg.tool_calls:
                    content: Any = self._assistant_content_blocks(
                        msg, reasoning_blocks
                    )
                else:
                    content = self._multimodal_content(msg)
                anthropic_messages.append(
                    {"role": "assistant", "content": content}
                )
                continue

            anthropic_messages.append(
                {"role": msg.role, "content": self._multimodal_content(msg)}
            )

        # Convert tools to Anthropic format
        anthropic_tools = None
        if tools:
            anthropic_tools = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]

        try:
            on_text_delta = kwargs.pop("on_text_delta", None)
            # Map the generic effort value onto Anthropic extended thinking
            # instead of leaking the OpenAI-style keyword into the SDK call.
            reasoning_effort = kwargs.pop("reasoning_effort", None)
            max_tokens_value = max_tokens or 4096
            request_kwargs = dict(
                model=model or self._model or "claude-3-5-sonnet-20241022",
                max_tokens=max_tokens_value,
                messages=anthropic_messages,
                system=system_prompt if system_prompt else None,
                tools=anthropic_tools,
                thinking=self._thinking_config(reasoning_effort, max_tokens_value),
                temperature=temperature,
                **kwargs,
            )
            # Drop kwargs the installed SDK no longer accepts (e.g. temperature
            # on newer releases) while keeping null optional fields out of the
            # wire payload.  The guard skips filtering when introspection cannot
            # see a real message signature (e.g. test doubles), in which case
            # every kwarg is forwarded unchanged.
            supported = self._supported_message_params()
            if {"model", "messages"}.issubset(supported):
                request_kwargs = {
                    key: value
                    for key, value in request_kwargs.items()
                    if key in supported and value is not None
                }
            if callable(on_text_delta):
                async with self._client.messages.stream(**request_kwargs) as stream:
                    async for text in stream.text_stream:
                        if not text:
                            continue
                        try:
                            emitted = on_text_delta(text)
                            if hasattr(emitted, "__await__"):
                                await emitted
                        except Exception:
                            pass
                    response = await stream.get_final_message()
            else:
                response = await self._client.messages.create(**request_kwargs)

            # Extract text, tool calls, and replayable thinking blocks.
            content = ""
            response_tool_calls: list[dict[str, Any]] = []
            reasoning_blocks: list[dict[str, Any]] = []
            reasoning_parts: list[str] = []

            for block in response.content:
                block_type = str(getattr(block, "type", "") or "")
                if block_type == "text":
                    content += str(getattr(block, "text", "") or "")
                elif block_type == "tool_use":
                    response_tool_calls.append(
                        {
                            "id": getattr(block, "id", ""),
                            "function": {
                                "name": getattr(block, "name", ""),
                                "arguments": getattr(block, "input", {}),
                            },
                        }
                    )
                elif block_type == "thinking":
                    thinking = str(getattr(block, "thinking", "") or "")
                    signature = str(getattr(block, "signature", "") or "")
                    if signature:
                        reasoning_blocks.append(
                            {
                                "type": "thinking",
                                "thinking": thinking,
                                "signature": signature,
                            }
                        )
                    if thinking:
                        reasoning_parts.append(thinking)
                elif block_type == "redacted_thinking":
                    data = str(getattr(block, "data", "") or "")
                    if data:
                        reasoning_blocks.append(
                            {"type": "redacted_thinking", "data": data}
                        )

            return ChatResponse(
                content=content,
                reported_model=getattr(response, "model", None),
                tool_calls=response_tool_calls or None,
                finish_reason=self._normalize_finish_reason(response.stop_reason),
                usage={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                reasoning_content="".join(reasoning_parts) or None,
                reasoning_blocks=reasoning_blocks or None,
            )
        except asyncio.CancelledError:
            raise
        except anthropic.APIError as e:
            raise Exception(f"Anthropic API error: {e}")

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatResponse]:
        # Reuse ``chat`` for exact tool reconstruction while forwarding text
        # deltas through a queue as soon as the SDK emits them.
        deltas: asyncio.Queue[str] = asyncio.Queue()

        async def collect(delta: str) -> None:
            deltas.put_nowait(delta)

        chat_task = asyncio.create_task(
            self.chat(
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                on_text_delta=collect,
                **kwargs,
            )
        )
        try:
            while not chat_task.done() or not deltas.empty():
                try:
                    delta = await asyncio.wait_for(deltas.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                yield ChatResponse(content=delta)
            response = await chat_task
            if (
                response.tool_calls
                or response.finish_reason
                or response.usage
                or response.reasoning_blocks
            ):
                yield ChatResponse(
                    content="",
                    tool_calls=response.tool_calls,
                    finish_reason=response.finish_reason,
                    usage=response.usage,
                    reasoning_content=response.reasoning_content,
                    reasoning_blocks=response.reasoning_blocks,
                )
        finally:
            if not chat_task.done():
                chat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await chat_task
