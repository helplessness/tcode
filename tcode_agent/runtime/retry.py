"""Retry and recovery helpers for runtime execution."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class RetryDecision:
    retryable: bool
    reason: str
    delay_seconds: float


_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "service unavailable",
    "rate limit",
    "too many requests",
    "502",
    "503",
    "504",
    "broken pipe",
    "eof",
)


def max_execution_retries() -> int:
    raw = str(os.environ.get("TCODE_EXECUTION_MAX_RETRIES", "1") or "1").strip()
    try:
        value = int(raw)
    except Exception:
        value = 1
    return max(0, min(3, value))


def _base_backoff_seconds() -> float:
    raw = str(os.environ.get("TCODE_RUNTIME_RETRY_BACKOFF_SECONDS", "1.5") or "1.5").strip()
    try:
        value = float(raw)
    except Exception:
        value = 1.5
    return min(max(0.2, value), 15.0)


def retry_decision_for_exception(exc: Exception, attempt: int) -> RetryDecision:
    text = str(exc or "").strip().lower()
    retryable = any(marker in text for marker in _TRANSIENT_MARKERS)
    reason = "检测到瞬时错误，准备自动恢复重试。" if retryable else "错误更像逻辑或配置问题，不自动重试。"
    delay = _base_backoff_seconds() * max(1, attempt + 1)
    return RetryDecision(retryable=retryable, reason=reason, delay_seconds=delay)
