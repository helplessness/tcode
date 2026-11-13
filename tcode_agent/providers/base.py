"""LLM Provider base class"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Literal, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import Usage


@dataclass
class ChatMessage:
    """Chat message for LLM"""

    role: str
    content: str
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None
    images: Optional[list[dict[str, Any]]] = None
    reasoning_content: Optional[str] = None
    # Opaque, provider-native reasoning items that must be replayed verbatim
    # across turns (for example Anthropic signed thinking blocks or OpenAI
    # Responses reasoning items).  Each provider only reads the shapes it
    # understands, so mixed-model histories stay safe.
    reasoning_blocks: Optional[list[dict[str, Any]]] = None
    # Marks a tool result as an error so Anthropic can set ``is_error``.
    is_error: Optional[bool] = None


@dataclass
class ChatResponse:
    """Response from LLM"""

    content: str
    tool_calls: Optional[list[dict]] = None
    finish_reason: Optional[str] = None
    usage: Optional[dict[str, int]] = None
    reasoning_content: Optional[str] = None
    reasoning_blocks: Optional[list[dict[str, Any]]] = None
    reported_model: Optional[str] = None  # 服务端回包中的模型身份；缺失不使用配置值冒充。


@dataclass
class ToolDefinition:
    """Tool definition for LLM"""

    name: str
    description: str
    parameters: dict[str, Any]
    # DSH-201A identity metadata is ignored by provider payload adapters.  It
    # exists so an immutable per-step snapshot can distinguish a definition
    # from the provider instance/version that owns it.
    provider_id: str = "legacy.unknown"
    provider_instance_id: str = ""
    provider_version: str = "unversioned"
    definition_version: str = "unversioned"

    def __post_init__(self) -> None:
        # MCP and plugin schemas are outside the host's trust boundary.  Some
        # servers publish enum/const-only property schemas, which are accepted
        # by a few model APIs but rejected by stricter OpenAI-compatible
        # implementations (notably Moonshot).  Complete the unambiguous JSON
        # Schema type without weakening constraints or mutating the provider's
        # cached definition.
        self.parameters = normalize_json_schema(self.parameters)


def normalize_json_schema(schema: Any) -> dict[str, Any]:
    """Return an isolated, minimally completed JSON Schema mapping.

    Only types that are implied by ``properties``, ``enum`` or ``const`` are
    added. Mixed-value enums remain untouched because guessing a union there
    could change validation semantics.
    """

    root = deepcopy(schema) if isinstance(schema, dict) else {}

    def inferred_type(values: list[Any]) -> Optional[str]:
        concrete = [value for value in values if value is not None]
        if not concrete:
            return "null" if values else None

        def value_type(value: Any) -> str:
            if isinstance(value, bool):
                return "boolean"
            if isinstance(value, int):
                return "integer"
            if isinstance(value, float):
                return "number"
            if isinstance(value, str):
                return "string"
            if isinstance(value, list):
                return "array"
            if isinstance(value, dict):
                return "object"
            return ""

        kinds = {value_type(value) for value in concrete}
        kinds.discard("")
        return next(iter(kinds)) if len(kinds) == 1 else None

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if "type" not in node:
            inferred: Optional[str] = None
            if isinstance(node.get("properties"), dict):
                inferred = "object"
            elif isinstance(node.get("enum"), list):
                inferred = inferred_type(node["enum"])
            elif "const" in node:
                inferred = inferred_type([node["const"]])
            if inferred:
                node["type"] = inferred
        for value in node.values():
            visit(value)

    visit(root)
    if "type" not in root and not any(
        keyword in root for keyword in ("anyOf", "oneOf", "allOf", "$ref")
    ):
        root["type"] = "object"
    return root


class LLMProvider(ABC):
    """Base class for LLM providers"""

    def __init__(self, api_key: str):
        self._api_key = api_key

    @property
    def stream_cancel_mode(
        self,
    ) -> Literal["task_cancel", "connection_close", "not_supported"]:
        """How an in-flight model request reacts to asyncio cancellation."""

        # Third-party providers inherit the conservative contract: the caller
        # may cancel its task, but no transport-level abort is promised.
        return "task_cancel"

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> ChatResponse:
        """Send chat request and return response"""
        pass

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolDefinition]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ):
        """Stream chat responses"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name"""
        pass
