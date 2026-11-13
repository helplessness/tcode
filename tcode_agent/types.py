"""Data types for TCode Agent"""

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Optional


class AgentMode(Enum):
    """Agent mode - primary, subagent, manager, worker, or all"""

    SUBAGENT = "subagent"
    PRIMARY = "primary"
    ALL = "all"
    MANAGER = "manager"  # TeamMode: manages Workers
    WORKER = "worker"    # TeamMode: receives tasks from Manager


class ToolState(Enum):
    """Tool execution state"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


@dataclass
class ToolCall:
    """Represents a tool call from the LLM"""

    tool_name: str
    call_id: str
    arguments: dict[str, Any]
    state: ToolState = ToolState.PENDING
    output: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    start_time: Optional[int] = None
    end_time: Optional[int] = None

    # Compaction metadata
    compacted_at: Optional[float] = None  # When this tool output was compacted/pruned


@dataclass
class ToolResult:
    """Result from tool execution"""

    title: str
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    output_path: Optional[str] = None
    error: Optional[str] = None


@dataclass
class Usage:
    """Token usage for a request/response"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __str__(self) -> str:
        return f"prompt={self.prompt_tokens}, completion={self.completion_tokens}, total={self.total_tokens}"

    def __add__(self, other: "Usage") -> "Usage":
        """Add two Usage objects together"""
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    @property
    def cost(self) -> float:
        """Estimate cost (simplified - real implementation would use model pricing)"""
        # Rough estimate: $1.5/1M input tokens, $15/1M output tokens
        return (self.prompt_tokens * 0.0015 + self.completion_tokens * 0.015) / 1000


@dataclass
class Message:
    """Chat message"""

    role: str  # "user", "assistant", "system", "tool"
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    parent_id: Optional[str] = None
    id: str = ""
    created_at: float = field(default_factory=time.time)
    error: Optional[dict] = None
    finish: Optional[str] = None
    usage: Optional[Usage] = None
    cost: float = 0
    process_notes: list[dict[str, Any]] = field(default_factory=list)
    presentation: Optional[str] = None  # Optional frontend presentation hint
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    research_grounding: dict[str, Any] = field(default_factory=dict)
    # Persisted per-message verification.  Internal contract identity is stored
    # here for revision invalidation; server presenters expose only status/confidence.
    verification: dict[str, Any] = field(default_factory=dict)
    # Model-visible images that belong to this exact user turn. Paths are
    # revalidated before every provider request; image bytes are never stored.
    input_images: list[dict[str, Any]] = field(default_factory=list)
    # Durable, structured message annotations. Server presenters must expose a
    # safe projection instead of forwarding arbitrary internal keys.
    metadata: dict[str, Any] = field(default_factory=dict)

    # Compaction metadata
    summary: bool = False  # Whether this message is a compaction summary
    compacted_at: Optional[float] = None  # When this message was compacted


@dataclass
class AgentInfo:
    """Agent configuration"""

    name: str
    description: str = ""
    mode: AgentMode = AgentMode.PRIMARY
    native: bool = False
    hidden: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    color: Optional[str] = None
    permission: dict[str, Any] = field(default_factory=dict)
    model: Optional[dict[str, str]] = None
    variant: Optional[str] = None
    prompt: Optional[str] = None
    options: dict[str, Any] = field(default_factory=dict)
    steps: Optional[int] = None


@dataclass
class PermissionRule:
    """Permission rule for tools"""

    permission: str
    action: str  # "allow", "deny", "ask"
    pattern: Optional[str] = None


@dataclass
class SessionInfo:
    """Session information"""

    id: str
    slug: str
    project_id: str
    directory: str
    title: str
    version: str
    time: dict[str, int] = field(default_factory=dict)
    permission: Optional[dict[str, Any]] = None
