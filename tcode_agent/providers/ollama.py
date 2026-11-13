"""Ollama provider for local LLM"""

import asyncio
import os
import json
from typing import Any, AsyncIterator, Optional

import aiohttp

from .base import LLMProvider, ChatMessage, ChatResponse, ToolDefinition


class OllamaProvider(LLMProvider):
    """Ollama local LLM provider"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3-coder-next:latest",
    ):
        super().__init__("")
        self._base_url = base_url
        self._model = model
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def stream_cancel_mode(self) -> str:
        # aiohttp closes the active response when its context exits.
        return "connection_close"

    @staticmethod
    def _ollama_message(role: str, content: str, images: Any = None) -> dict[str, Any]:
        message: dict[str, Any] = {"role": role, "content": content}
        encoded_images = []
        for image in list(images or [])[:2]:
            data_url = str(image.get("data_url") or "").strip()
            if data_url.startswith("data:") and ";base64," in data_url:
                encoded_images.append(data_url.split(",", 1)[1])
        if encoded_images:
            message["images"] = encoded_images
        return message

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=180)
            )
        return self._session

    async def close(self):
        if self._session:
            if not self._session.closed:
                await self._session.close()
            self._session = None

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        # Convert messages to Ollama format (handle both ChatMessage and dict)
        ollama_messages = []
        for msg in messages:
            # Handle both ChatMessage objects and dicts
            role = msg.role if hasattr(msg, "role") else msg.get("role", "user")
            content = msg.content if hasattr(msg, "content") else msg.get("content", "")
            images = msg.images if hasattr(msg, "images") else msg.get("images", [])

            if role == "system":
                ollama_messages.append(self._ollama_message("system", content))
            elif role == "tool":
                ollama_messages.append(self._ollama_message("user", content))
            else:
                ollama_messages.append(self._ollama_message(role, content, images))

        # Build request
        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": ollama_messages,
            "stream": False,
        }

        if temperature is not None:
            payload["options"] = {"temperature": temperature}
        if max_tokens is not None:
            if "options" not in payload:
                payload["options"] = {}
            payload["options"]["num_predict"] = max_tokens

        # Add tools if provided
        if tools:
            payload["tools"] = [
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

        # Retry logic for transient errors
        max_retries = 3

        for attempt in range(max_retries):
            try:
                # Reset session if closed
                if self._session and self._session.closed:
                    self._session = None

                session = await self._get_session()
                async with session.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise Exception(f"Ollama error {resp.status}: {error_text}")

                    data = await resp.json()

                    # Extract content
                    content = ""
                    response_tool_calls = None

                    message = data.get("message", {})
                    content = message.get("content", "")

                    # Check for tool calls (if model called a tool)
                    if message.get("tool_calls"):
                        response_tool_calls = []
                        for tc in message["tool_calls"]:
                            response_tool_calls.append(
                                {
                                    "id": tc.get("id", ""),
                                    "function": {
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": tc.get("function", {}).get(
                                            "arguments", {}
                                        ),
                                    },
                                }
                            )

                    return ChatResponse(
                        content=content,
                        tool_calls=response_tool_calls,
                        finish_reason=data.get("done_reason"),
                        usage={
                            "prompt_tokens": data.get("prompt_eval_count", 0),
                            "completion_tokens": data.get("eval_count", 0),
                            "total_tokens": data.get("prompt_eval_count", 0)
                            + data.get("eval_count", 0),
                        },
                    )
            except asyncio.CancelledError:
                raise
            except aiohttp.ClientError as e:
                # Reset session on error (close old one first to avoid leaks)
                if self._session and not self._session.closed:
                    await self._session.close()
                self._session = None
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                raise Exception(
                    f"Ollama connection error after {max_retries} retries: {e}"
                )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatResponse]:
        # Convert messages to Ollama format
        ollama_messages = []
        for msg in messages:
            if msg.role == "system":
                ollama_messages.append(self._ollama_message("system", msg.content))
            elif msg.role == "tool":
                ollama_messages.append(self._ollama_message("user", msg.content))
            else:
                ollama_messages.append(
                    self._ollama_message(msg.role, msg.content, msg.images)
                )

        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": ollama_messages,
            "stream": True,
        }

        if temperature is not None:
            payload["options"] = {"temperature": temperature}
        if max_tokens is not None:
            if "options" not in payload:
                payload["options"] = {}
            payload["options"]["num_predict"] = max_tokens

        try:
            session = await self._get_session()
            async with session.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Ollama error {resp.status}: {error_text}")

                async for line in resp.content:
                    if line:
                        try:
                            data = json.loads(line)
                            if "message" in data:
                                content = data["message"].get("content", "")
                                yield ChatResponse(
                                    content=content,
                                    tool_calls=None,
                                    finish_reason=None,
                                )
                        except json.JSONDecodeError:
                            pass
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError as e:
            raise Exception(f"Ollama connection error: {e}")

    @classmethod
    def list_models(cls, base_url: str = "http://localhost:11434") -> list[str]:
        """List available models (synchronous)"""
        import urllib.request
        import urllib.error

        try:
            req = urllib.request.Request(f"{base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read())
                return [model["name"] for model in data.get("models", [])]
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            return []
