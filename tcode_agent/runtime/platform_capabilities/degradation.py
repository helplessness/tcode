"""Stable degradation reasons exposed to clients and Harnesses."""

from __future__ import annotations


DEGRADATION_REASONS = {
    "bridge_disabled": "TCode platform bridge is disabled by server policy.",
    "runtime_tools_unavailable": "The selected Harness has no verified tool capability.",
    "native_image_input": "The selected model and Harness accept image input directly.",
    "vision_model_unavailable": "No admitted TCode vision model is configured.",
    "skills_not_admitted": "No Skills were admitted for this Run.",
    "wiki_unavailable": "The workspace Wiki is unavailable or disabled.",
    "job_protocol_unavailable": "The Job Protocol facade is unavailable.",
}


def degradation_reason(code: str) -> str:
    return DEGRADATION_REASONS.get(code, str(code or "Capability is unavailable."))


__all__ = ["DEGRADATION_REASONS", "degradation_reason"]
