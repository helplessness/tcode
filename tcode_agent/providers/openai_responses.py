"""OpenAI **Responses API** wire adapter for the OpenAI-compatible provider.

This module translates between the host's chat-completions-shaped
``ChatMessage`` / ``ToolDefinition`` / ``ChatResponse`` contract and the newer
``POST /responses`` protocol. It is deliberately self-contained so the existing
Chat Completions path stays untouched.

Design notes (verified against DeepSeek V4):

* Responses is *stateless for us*: the host already replays the full history
  every turn, so we never rely on ``previous_response_id``/``store``.
  ``store`` is forced to ``False``.
* Reasoning items are not replayed. Re-sending them (with or without
  ``encrypted_content``) is accepted but unnecessary, and dropping them keeps
  the request equivalent to the chat-completions history.
* ``status == "incomplete"`` with ``incomplete_details.reason ==
  "max_output_tokens"`` must become ``finish_reason="length"`` or the host's
  output-budget guard would execute a truncated turn.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import openai

from .base import ChatMessage, ChatResponse, ToolDefinition


API_MODE_CHAT = "chat_completions"
API_MODE_RESPONSES = "responses"
API_MODE_AUTO = "auto"
SUPPORTED_API_MODES = (API_MODE_CHAT, API_MODE_RESPONSES, API_MODE_AUTO)

_MODE_ALIASES = {
    "chat": API_MODE_CHAT,
    "chat_completion": API_MODE_CHAT,
    "chat_completions": API_MODE_CHAT,
    "completions": API_MODE_CHAT,
    "completion": API_MODE_CHAT,
    "openai_chat": API_MODE_CHAT,
    "responses": API_MODE_RESPONSES,
    "response": API_MODE_RESPONSES,
    "responses_api": API_MODE_RESPONSES,
    "openai_responses": API_MODE_RESPONSES,
    "auto": API_MODE_AUTO,
}

# Extra kwargs that only make sense for the chat-completions wire shape.
_CHAT_ONLY_KEYS = frozenset(
    {"stream_options", "max_completion_tokens"}
)

_ENDPOINT_UNSUPPORTED_SIGNALS = (
    "unknown endpoint",
    "unsupported endpoint",
    "unknown url",
    "no such endpoint",
    "does not support the responses api",
    "responses api is not supported",
    "not implemented",
)

_IMAGE_UNSUPPORTED_SIGNALS = (
    "input_image",
    "image_url",
    "image input",
    "image content",
    "image[0]",
    "unsupported image",
    "invalid image",
    "multimodal",
    "unknown variant `image",
    'unknown variant "image',
)


class ResponsesApiError(RuntimeError):
    """Raised when a Responses response reports a terminal failure."""


def normalize_api_mode(value: Any) -> str:
    """Return a canonical api mode, or ``""`` when unrecognised."""

    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    if raw in _MODE_ALIASES:
        return _MODE_ALIASES[raw]
    if "responses" in raw:
        return API_MODE_RESPONSES
    if "compatible" in raw or "chat" in raw or "completion" in raw:
        return API_MODE_CHAT
    return ""


def _image_omitted_note(*, required_image_inputs: bool) -> str:
    if required_image_inputs:
        return (
            "[IMAGE_INPUT_ROUTING: Direct image input was rejected by "
            "this model endpoint. The uploaded image was not discarded. "
            "Use the look_at tool with the workspace path shown in the "
            "user message before answering questions that depend on image "
            "content. If visual inspection is unavailable, respond with a "
            "friendly capability diagnosis; do not expose the provider error.]"
        )
    return (
        "[Browser observation image omitted because this model endpoint "
        "does not accept image input. Rely on the structured browser "
        "observation and current page_revision above.]"
    )


def _stringify_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _content_with_images(message: ChatMessage) -> Optional[list[dict[str, Any]]]:
    images = [
        image
        for image in list(message.images or [])[:2]
        if isinstance(image, dict) and str(image.get("data_url") or "").strip()
    ]
    if not images:
        return None
    parts: list[dict[str, Any]] = []
    if str(message.content or "").strip():
        parts.append({"type": "input_text", "text": str(message.content)})
    for image in images:
        parts.append(
            {
                "type": "input_image",
                "image_url": str(image.get("data_url")),
                "detail": str(image.get("detail") or "low"),
            }
        )
    return parts


def _as_input_message(message: ChatMessage) -> dict[str, Any]:
    content = _content_with_images(message)
    role = str(message.role or "user").strip().lower() or "user"
    if content is not None:
        return {"role": role, "content": content}
    return {"role": role, "content": str(message.content or "")}


def build_responses_payload(
    messages: list[ChatMessage],
    tools: Optional[list[ToolDefinition]],
    *,
    model: str,
    temperature: Optional[float],
    max_tokens: Optional[int],
    reasoning_effort: Optional[str],
    supports_reasoning_effort: bool,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a ``POST /responses`` request body from host chat messages."""

    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []

    for message in messages:
        role = str(message.role or "").strip().lower()
        if role == "system":
            text = str(message.content or "").strip()
            if text:
                instructions.append(text)
            continue

        if role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.tool_call_id or ""),
                    "output": str(message.content or ""),
                }
            )
            continue

        if role == "assistant" and message.tool_calls:
            content = _content_with_images(message)
            if content is not None:
                input_items.append({"role": "assistant", "content": content})
            elif str(message.content or "").strip():
                input_items.append({"role": "assistant", "content": str(message.content)})
            for tool_call in message.tool_calls:
                function = (
                    tool_call.get("function")
                    if isinstance(tool_call.get("function"), dict)
                    else {}
                )
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": str(
                            tool_call.get("id") or tool_call.get("call_id") or ""
                        ),
                        "name": str(function.get("name") or ""),
                        "arguments": _stringify_arguments(function.get("arguments")),
                    }
                )
            continue

        input_items.append(_as_input_message(message))

    payload: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "store": False,
    }
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_tokens is not None:
        payload["max_output_tokens"] = int(max_tokens)
    if reasoning_effort and supports_reasoning_effort:
        payload["reasoning"] = {"effort": str(reasoning_effort)}

    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]

    for key, value in dict(extra or {}).items():
        if key in _CHAT_ONLY_KEYS or key == "reasoning_effort" or value is None:
            continue
        if key == "response_format":
            if isinstance(value, dict) and value.get("type") == "json_object":
                payload["text"] = {"format": {"type": "json_object"}}
            continue
        payload[key] = value
    return payload


def payload_has_image_inputs(payload: dict[str, Any]) -> bool:
    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "input_image"
            for part in content
        ):
            return True
    return False


def without_image_inputs(
    payload: dict[str, Any],
    *,
    required_image_inputs: bool = False,
) -> dict[str, Any]:
    """Return a copy with ``input_image`` parts replaced by the same text note
    the chat-completions path uses, so image-fallback behavior is identical."""

    fallback = dict(payload)
    note = _image_omitted_note(required_image_inputs=required_image_inputs)
    new_items: list[Any] = []
    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            new_items.append(item)
            continue
        content = item.get("content")
        if not isinstance(content, list) or not any(
            isinstance(part, dict) and part.get("type") == "input_image"
            for part in content
        ):
            new_items.append(dict(item))
            continue
        text_parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"input_text", "text", "output_text"}
        ]
        text_content = "\n".join(part for part in text_parts if part.strip()).strip()
        cloned = dict(item)
        cloned["content"] = f"{text_content}\n\n{note}".strip()
        new_items.append(cloned)
    fallback["input"] = new_items
    return fallback


def estimate_payload_chars(payload: dict[str, Any]) -> int:
    """Estimate prompt text size, mirroring the chat-completions accounting."""

    total = len(str(payload.get("instructions") or ""))

    def count_content(content: Any) -> int:
        if isinstance(content, str):
            return len(content)
        subtotal = 0
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    subtotal += len(str(part))
                elif part.get("type") in {"input_text", "text", "output_text"}:
                    subtotal += len(str(part.get("text") or ""))
                elif part.get("type") == "input_image":
                    # Image token accounting is model-specific; never count the
                    # base64 transport bytes as prompt text.
                    subtotal += 1024
        return subtotal

    for item in payload.get("input") or []:
        if not isinstance(item, dict):
            total += len(str(item))
            continue
        item_type = str(item.get("type") or "")
        if item_type == "function_call":
            total += len(str(item.get("name") or ""))
            total += len(str(item.get("arguments") or ""))
            total += len(str(item.get("call_id") or ""))
            continue
        if item_type == "function_call_output":
            total += len(str(item.get("output") or ""))
            total += len(str(item.get("call_id") or ""))
            continue
        total += len(str(item.get("role") or ""))
        total += count_content(item.get("content"))
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            total += len(str(tool))
            continue
        total += len(str(tool.get("name") or ""))
        total += len(str(tool.get("description") or ""))
        try:
            total += len(json.dumps(tool.get("parameters") or {}, ensure_ascii=False))
        except (TypeError, ValueError):
            total += len(str(tool.get("parameters")))
    return total


def is_image_input_unsupported(exc: Exception) -> bool:
    detail = str(exc or "").lower()
    return any(signal in detail for signal in _IMAGE_UNSUPPORTED_SIGNALS)


def is_endpoint_unsupported(exc: Exception) -> bool:
    """Best-effort detector for "this server has no /responses endpoint"."""

    if isinstance(exc, openai.NotFoundError):
        return True
    status = getattr(exc, "status_code", None)
    if status in (404, 405):
        return True
    detail = str(exc or "").lower()
    if any(signal in detail for signal in _ENDPOINT_UNSUPPORTED_SIGNALS):
        return True
    if ("404" in detail or "405" in detail) and "responses" in detail:
        return True
    return False


def _output_items(response: Any) -> list[Any]:
    output = getattr(response, "output", None)
    return list(output) if isinstance(output, (list, tuple)) else []


def _collect_output_text(response: Any) -> str:
    convenience = getattr(response, "output_text", None)
    if isinstance(convenience, str) and convenience:
        return convenience
    parts: list[str] = []
    for item in _output_items(response):
        if str(getattr(item, "type", "") or "") != "message":
            continue
        for part in getattr(item, "content", None) or []:
            part_type = str(getattr(part, "type", "") or "")
            if part_type in {"output_text", "text"}:
                parts.append(str(getattr(part, "text", "") or ""))
    return "".join(parts)


def _collect_reasoning_text(response: Any) -> str:
    parts: list[str] = []
    for item in _output_items(response):
        if str(getattr(item, "type", "") or "") != "reasoning":
            continue
        for part in getattr(item, "content", None) or []:
            if str(getattr(part, "type", "") or "") in {
                "reasoning_text",
                "summary_text",
                "text",
            }:
                parts.append(str(getattr(part, "text", "") or ""))
        for part in getattr(item, "summary", None) or []:
            text = getattr(part, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts)


def _collect_tool_calls(response: Any) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for item in _output_items(response):
        if str(getattr(item, "type", "") or "") != "function_call":
            continue
        tool_calls.append(
            {
                "id": str(getattr(item, "call_id", "") or getattr(item, "id", "") or ""),
                "function": {
                    "name": str(getattr(item, "name", "") or ""),
                    "arguments": str(getattr(item, "arguments", "") or ""),
                },
            }
        )
    return tool_calls


def finish_reason_for(response: Any) -> str:
    """Map a Responses terminal state to a chat-completions finish_reason."""

    status = str(getattr(response, "status", "") or "").strip().lower()
    if status == "failed":
        return "error"
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = ""
        if details is not None:
            reason = str(getattr(details, "reason", "") or "").strip().lower()
        if reason in {"max_output_tokens", "max_tokens", "length"}:
            return "length"
        if reason in {"content_filter", "content_filtered"}:
            return "content_filter"
        return reason or "length"
    has_tool_call = any(
        str(getattr(item, "type", "") or "") == "function_call"
        for item in _output_items(response)
    )
    return "tool_calls" if has_tool_call else "stop"


def usage_dict_for(response: Any) -> Optional[dict[str, int]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def response_error_text(response: Any) -> str:
    error = getattr(response, "error", None)
    if error is None:
        return "Responses API returned a failed response"
    if isinstance(error, dict):
        return str(error.get("message") or error)
    message = getattr(error, "message", None)
    return str(message or error)


def response_to_chat_response(response: Any) -> ChatResponse:
    """Normalize a non-streaming Responses object to ``ChatResponse``."""

    if str(getattr(response, "status", "") or "").strip().lower() == "failed":
        raise ResponsesApiError(response_error_text(response))
    return ChatResponse(
        content=_collect_output_text(response),
        reported_model=getattr(response, "model", None),
        reasoning_content=_collect_reasoning_text(response) or None,
        tool_calls=_collect_tool_calls(response) or None,
        finish_reason=finish_reason_for(response),
        usage=usage_dict_for(response),
    )


class ResponsesStreamState:
    """Accumulate Responses SSE events into chat-completions-shaped deltas."""

    def __init__(self) -> None:
        self._tool_ids: dict[int, str] = {}
        self._tool_names: dict[int, str] = {}
        self._arguments_seen: dict[int, bool] = {}
        self.finish_reason: Optional[str] = None
        self.usage: Optional[dict[str, int]] = None
        self.failed_error: str = ""

    def feed(self, event: Any) -> list[ChatResponse]:
        event_type = str(getattr(event, "type", "") or "")
        if event_type == "response.output_text.delta":
            delta = str(getattr(event, "delta", "") or "")
            return [ChatResponse(content=delta)] if delta else []
        if event_type in {
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        }:
            delta = str(getattr(event, "delta", "") or "")
            return [ChatResponse(content="", reasoning_content=delta)] if delta else []
        if event_type == "response.output_item.added":
            return self._on_output_item_added(event)
        if event_type == "response.function_call_arguments.delta":
            return self._on_arguments_delta(event)
        if event_type == "response.output_item.done":
            return self._on_output_item_done(event)
        if event_type in {
            "response.completed",
            "response.incomplete",
            "response.failed",
        }:
            return self._on_terminal(event)
        return []

    def _on_output_item_added(self, event: Any) -> list[ChatResponse]:
        item = getattr(event, "item", None)
        if str(getattr(item, "type", "") or "") != "function_call":
            return []
        index = int(getattr(event, "output_index", 0) or 0)
        call_id = str(getattr(item, "call_id", "") or "")
        name = str(getattr(item, "name", "") or "")
        self._tool_ids.setdefault(index, call_id)
        self._tool_names.setdefault(index, name)
        arguments = str(getattr(item, "arguments", "") or "")
        if arguments:
            self._arguments_seen[index] = True
        return [
            ChatResponse(
                content="",
                tool_calls=[
                    {
                        "index": index,
                        "id": call_id,
                        "function": {"name": name, "arguments": arguments},
                    }
                ]
            )
        ]

    def _on_arguments_delta(self, event: Any) -> list[ChatResponse]:
        index = int(getattr(event, "output_index", 0) or 0)
        delta = str(getattr(event, "delta", "") or "")
        self._arguments_seen[index] = True
        if not delta:
            return []
        return [
            ChatResponse(
                content="",
                tool_calls=[
                    {
                        "index": index,
                        "id": "",
                        "function": {"name": "", "arguments": delta},
                    }
                ]
            )
        ]

    def _on_output_item_done(self, event: Any) -> list[ChatResponse]:
        """Backfill id/name if a gateway skipped the ``output_item.added`` step."""

        item = getattr(event, "item", None)
        if str(getattr(item, "type", "") or "") != "function_call":
            return []
        index = int(getattr(event, "output_index", 0) or 0)
        call_id = str(getattr(item, "call_id", "") or "")
        name = str(getattr(item, "name", "") or "")
        missing_id = call_id and not self._tool_ids.get(index)
        missing_name = name and not self._tool_names.get(index)
        if not missing_id and not missing_name:
            return []
        if missing_id:
            self._tool_ids[index] = call_id
        if missing_name:
            self._tool_names[index] = name
        return [
            ChatResponse(
                content="",
                tool_calls=[
                    {
                        "index": index,
                        "id": call_id if missing_id else "",
                        "function": {
                            "name": name if missing_name else "",
                            "arguments": "",
                        },
                    }
                ]
            )
        ]

    def _on_terminal(self, event: Any) -> list[ChatResponse]:
        event_type = str(getattr(event, "type", "") or "")
        response = getattr(event, "response", None)
        if event_type == "response.failed":
            self.failed_error = response_error_text(response)
        self.finish_reason = finish_reason_for(response) if response is not None else "stop"
        self.usage = usage_dict_for(response)
        return [
            ChatResponse(
                content="",
                finish_reason=self.finish_reason,
                reported_model=getattr(response, "model", None),
                usage=self.usage,
            )
        ]
