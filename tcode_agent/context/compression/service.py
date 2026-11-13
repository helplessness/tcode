"""Compress only message projections; failures preserve the existing path."""
from dataclasses import replace
import hashlib
import json
import re
import time
from .contracts import Owner, Policy
from .reducers import reduce_content
from .store import CapsuleStore


_METRIC_FIELDS = {
    "tool", "mode", "reason", "content_kind", "raw_chars",
    "projected_chars", "latency_ms", "source_stage", "source_complete",
}


def shadow_measurement(text: str, tool: str, policy: Policy, max_chars: int):
    """Measure a candidate without storing or returning source content."""
    if not policy.admits(tool) or len(text) < policy.min_chars:
        return None
    if len(text.encode()) > policy.max_bytes or max_chars < 1000:
        return None
    began = time.monotonic()
    reason, kind, projected_chars = "no_gain", "unsupported", len(text)
    try:
        reduced = reduce_content(
            text,
            max_chars - 700,
            began + policy.max_latency_ms / 1000,
        )
        if reduced:
            kind = reduced.kind
            preview = json.dumps(
                {
                    "schema": "tcode.context-capsule/v1",
                    "capsule_id": "ctx_shadow",
                    "source_complete": True,
                    "untrusted_data": True,
                    "strategy": reduced.kind + "-v1",
                    "notice": "Partial observation, not exhaustive.",
                    "retrieval": {
                        "tool": "context_expand",
                        "range": "zero_based_end_exclusive",
                    },
                    "data": reduced.view,
                },
                ensure_ascii=False,
            )
            if len(preview) <= min(max_chars, len(text) * .9):
                reason, projected_chars = "compressed", len(preview)
    except Exception as exc:
        reason = "fallback:" + type(exc).__name__
    return {
        "tool": tool,
        "mode": "shadow",
        "reason": reason,
        "content_kind": kind,
        "raw_chars": len(text),
        "projected_chars": projected_chars,
        "latency_ms": round((time.monotonic() - began) * 1000, 2),
        "source_stage": "before_hard_limit",
    }


def _safe_shadow_metric(value, tool: str):
    if not isinstance(value, dict) or value.get("mode") != "shadow":
        return None
    metric = {key: value[key] for key in _METRIC_FIELDS if key in value}
    if metric.get("tool") != tool:
        return None
    return metric


class CompressionService:
    def __init__(self, workdir: str, owner: Owner, policy: Policy, max_chars: int = 6000):
        self.workdir, self.owner, self.policy = workdir, owner, policy
        self.max_chars = max_chars
        self.metrics = []
        self.cache = {}

    def project(self, message, max_chars: int):
        max_chars = min(max_chars, self.max_chars)
        if message.role != "tool" or len(message.tool_calls) != 1:
            return message
        call = message.tool_calls[0]
        tool = call.tool_name
        metadata = {**call.metadata, **message.metadata}
        if self.policy.mode == "shadow":
            measured = _safe_shadow_metric(
                metadata.get("context_compression_shadow"), tool
            )
            if measured is not None and self.policy.admits(tool):
                self.metrics.append(measured)
                return message
        if (not self.policy.admits(tool) or metadata.get("compression_applied")
                or metadata.get("compression_risk") in {"high", "critical"}
                or metadata.get("compression_disabled")):
            return message
        text = message.content
        source_id = metadata.get("context_source_id")
        if source_id and self.policy.mode == "enforce":
            try:
                text = CapsuleStore(self.workdir, self.owner, self.policy).load(source_id)
            except Exception:
                return message
        if len(text) < self.policy.min_chars or len(text.encode()) > self.policy.max_bytes or max_chars < 1000:
            return message
        key = (id(message), max_chars, hashlib.sha256(text.encode()).hexdigest(),
               source_id, metadata.get('context_source_complete'), metadata.get('truncated'))
        if key in self.cache:
            return self.cache[key]
        began = time.monotonic()
        reason = "no_gain"
        candidate = message
        try:
            reduced = reduce_content(text, max_chars-700, began + self.policy.max_latency_ms / 1000)
            if reduced or (self.policy.mode == 'enforce' and (source_id or len(text) > max_chars)):
                # Store only in enforce; shadow has no content persistence side effect.
                capsule_id = "ctx_shadow" if self.policy.mode == "shadow" else source_id or CapsuleStore(self.workdir, self.owner, self.policy).put(text, {
                    "message_id": message.id, "tool_call_id": call.call_id, "tool": tool,
                    "source_complete": metadata.get("context_source_complete", not metadata.get("truncated", False))})
                view = {"schema": "tcode.context-capsule/v1", "capsule_id": capsule_id,
                        "source_complete": metadata.get("context_source_complete", not metadata.get("truncated", False)),
                        "untrusted_data": True, "strategy": reduced.kind + "-v1" if reduced else 'reference-only-v1',
                        "notice": ("Partial observation, not exhaustive. " if reduced else "No safe summary fits; the received source is preserved. ") + "Use context_expand to inspect omitted evidence before conclusions about all records.",
                        "retrieval": {"tool": "context_expand", "range": "zero_based_end_exclusive"},
                        "data": reduced.view if reduced else {'summary_available': False, 'raw_chars': len(text)}}
                if not reduced:
                    # Verbatim navigation only: an outline is not a summary or proof.
                    outline = []
                    source_sections = []
                    for index, line in enumerate(text.splitlines()):
                        if re.match(r'^(?:\d+: )?来源文件[：:]', line) and len(line) <= 180:
                            source_sections.append({'index': index, 'text': line})
                        if re.match(r'^(?:\d+: )?(?:#{1,6} |来源文件[：:])', line) and len(line) <= 180:
                            if sum(len(item['text']) for item in outline) + len(line) > min(1600, max(0, max_chars - 1100)):
                                continue
                            outline.append({'index': index, 'text': line})
                    view['data'].update(outline=outline, outline_exhaustive=False, source_lines=len(text.splitlines()))
                    if source_sections:
                        view['data'].update(source_sections=source_sections[:12], source_sections_exhaustive=len(source_sections)<=12)
                    view['retrieval'].update(max_calls_per_turn=self.policy.max_calls, guidance='Known terms: query first, then bounded line_range. A partial source cannot contain unread file pages.')
                content = json.dumps(view, ensure_ascii=False)
                if len(content) > max_chars and not reduced:
                    view['data'].pop('outline', None)
                    view['data'].pop('source_sections', None)
                    view['data'].pop('source_sections_exhaustive', None)
                    view['retrieval'].pop('guidance', None)
                    content = json.dumps(view, ensure_ascii=False)
                if len(content) <= min(max_chars, len(text)*.9):
                    reason = "compressed"
                    if self.policy.mode == "enforce":
                        candidate = replace(message, content=content, metadata={**message.metadata, "compression_applied": True})
                    measured_chars = len(content)
                else:
                    measured_chars = len(text)
            else:
                measured_chars = len(text)
        except Exception as exc:
            reason = "fallback:" + type(exc).__name__
            measured_chars = len(text)
        self.metrics.append({"tool": tool, "mode": self.policy.mode, "reason": reason,
                             "raw_chars": len(text), "projected_chars": measured_chars,
                             "latency_ms": round((time.monotonic()-began)*1000, 2)})
        self.cache[key] = candidate
        return candidate
