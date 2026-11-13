"""Transport bindings only; domain parsing belongs to workflow plugins.

Bindings execute no imports, regexes, tools, models or scripts. Apply them only
when starting a new run, before applicability checks, never when resuming it.
"""
from __future__ import annotations

from typing import Any


def request_binding_field(input_contract: dict[str, Any] | None) -> str:
    contract = input_contract if isinstance(input_contract, dict) else {}
    fields: set[str] = set()
    legacy_request = False
    for group in ("required", "optional"):
        for item in contract.get(group, []) or []:
            if isinstance(item, str):
                legacy_request |= item == "natural_language_request"
                continue
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or item.get("name") or item.get("key") or "").strip()
            legacy_request |= field == "natural_language_request"
            if "source" not in item:
                continue
            if item["source"] != "user_request" or not field:
                raise ValueError("workflow input source must be user_request on a named field")
            fields.add(field)
    if len(fields) > 1:
        raise ValueError("workflow input contract may bind user_request to only one field")
    return next(iter(fields), "natural_language_request" if legacy_request else "")


def latest_user_request(messages: list[Any] | None) -> str:
    for message in reversed(messages or []):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if role == "user" and isinstance(content, str) and content.strip():
            return content
    return ""


def bind_workflow_request(
    payload: dict[str, Any], input_contract: dict[str, Any] | None, user_request: str,
) -> dict[str, Any]:
    """Bind authoritative chat text, not a model paraphrase; keep other fields.

    Without chat text (e.g. a structured API call), preserve the caller's input.
    The plugin must still validate its actual domain input before using it.
    """
    field = request_binding_field(input_contract)
    if field and isinstance(user_request, str) and user_request.strip():
        return {**payload, field: user_request}
    return dict(payload)
