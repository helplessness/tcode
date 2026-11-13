"""Postflight checks after task execution."""

from __future__ import annotations

from typing import Any


def run_postflight_checks(
    *,
    result_text: str,
    review: dict[str, Any],
) -> dict[str, Any]:
    normalized_result = str(result_text or "").strip()
    verdict = str(review.get("verdict", "") or "").strip().upper()
    checks: list[dict[str, Any]] = [
        {
            "name": "result_non_empty",
            "status": "pass" if normalized_result else "warn",
            "detail": f"length={len(normalized_result)}",
        },
        {
            "name": "review_verdict_present",
            "status": "pass" if verdict else "warn",
            "detail": verdict or "missing",
        },
        {
            "name": "verifier_summary_present",
            "status": "pass"
            if str(review.get("verifier", {}).get("summary", "") or "").strip()
            else "warn",
            "detail": "verifier summary",
        },
    ]
    has_warn = any(item.get("status") == "warn" for item in checks)
    return {
        "status": "warn" if has_warn else "pass",
        "summary": "后检发现轻微告警。" if has_warn else "后检通过，结果可归档。",
        "checks": checks,
    }
