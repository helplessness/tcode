"""Identity and error contracts shared by in-process platform knowledge adapters."""

from __future__ import annotations

from dataclasses import dataclass


class PlatformKnowledgeError(RuntimeError):
    code = "platform_knowledge_error"


class PlatformKnowledgeAccessError(PlatformKnowledgeError):
    code = "platform_knowledge_access_denied"


class PlatformKnowledgeValidationError(PlatformKnowledgeError):
    code = "platform_knowledge_invalid_request"


@dataclass(frozen=True, slots=True)
class KnowledgeOwner:
    tenant_id: str
    user_id: str
    workspace_id: str
    workdir: str
    session_id: str = ""
    run_id: str = ""
    runtime_id: str = ""

    def __post_init__(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (self.tenant_id, self.user_id, self.workspace_id, self.workdir)
        ):
            raise PlatformKnowledgeAccessError("Knowledge access requires a complete owner and workspace")


__all__ = [
    "KnowledgeOwner",
    "PlatformKnowledgeAccessError",
    "PlatformKnowledgeError",
    "PlatformKnowledgeValidationError",
]
