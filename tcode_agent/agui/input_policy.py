"""公开 AG-UI 请求的大小、深度和字段策略。"""

from __future__ import annotations

import json
import os
from typing import Any


class InputPolicyError(ValueError):
    pass


def _depth(value: Any, current: int = 0) -> int:
    if current > 32:
        return current
    if isinstance(value, dict):
        return max([current, *(_depth(item, current + 1) for item in value.values())])
    if isinstance(value, list):
        return max([current, *(_depth(item, current + 1) for item in value)])
    return current


def validate_public_input(value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    max_bytes = max(16_384, min(int(os.environ.get("TCODE_AGUI_MAX_REQUEST_BYTES", "2097152")), 16_777_216))
    if len(encoded) > max_bytes:
        raise InputPolicyError(f"request exceeds {max_bytes} bytes")
    max_depth = max(4, min(int(os.environ.get("TCODE_AGUI_MAX_JSON_DEPTH", "12")), 32))
    if _depth(value) > max_depth:
        raise InputPolicyError(f"request exceeds JSON depth {max_depth}")


__all__ = ["InputPolicyError", "validate_public_input"]
