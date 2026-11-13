"""Turn-local evidence ledger derived from tool results and artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


_EVIDENCE_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,}")


def _evidence_tokens(value: Any) -> set[str]:
    return {
        token.lower()
        for token in _EVIDENCE_TOKEN_RE.findall(str(value or ""))
        if len(token.strip()) >= 2
    }


@dataclass
class EvidenceEntry:
    tool_name: str
    call_id: str = ""
    status: str = "completed"
    content_preview: str = ""
    content_hash: str = ""
    output_path: str = ""
    truncated: bool = False
    references: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceLedger:
    entries: list[EvidenceEntry] = field(default_factory=list)

    @classmethod
    def from_messages(
        cls,
        messages: list[Any],
        *,
        preview_chars: int = 1200,
    ) -> "EvidenceLedger":
        entries: list[EvidenceEntry] = []
        for message in messages or []:
            if str(getattr(message, "role", "") or "") != "tool":
                continue
            content = str(getattr(message, "content", "") or "")
            digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
            for tool_call in list(getattr(message, "tool_calls", []) or []):
                tool_name = str(getattr(tool_call, "tool_name", "") or "")
                limit = max(preview_chars, 16_000) if tool_name in {"intelligent_page", "canvas", "knowledge_search"} else preview_chars
                metadata = dict(getattr(tool_call, "metadata", {}) or {})
                if metadata.get("hypothesis_map") or metadata.get("research_changes"):
                    limit = max(preview_chars, 16_000)
                state = getattr(tool_call, "state", None)
                state_value = str(getattr(state, "value", state) or "").strip().lower()
                error = str(getattr(tool_call, "error", "") or "").strip()
                refs = {
                    key: metadata.get(key)
                    for key in (
                        "task_id",
                        "session_id",
                        "doc_id",
                        "record_path",
                        "downloaded_pdf_paths",
                        "artifacts",
                        "tool_execution_log_paths",
                        "research_grounding",
                        "hypothesis_map",
                        "research_changes",
                        "sources",
                        "approval_id",
                        "approval_status",
                        "provider_receipt",
                        "guidance_ids",
                        "semantic_disposition",
                        "superseded_by_guidance",
                        "argument_validation",
                        "execution_skipped",
                    )
                    if metadata.get(key) not in (None, "", [], {})
                }
                # Bind composition recovery to the same intended scene, not an
                # unrelated successful read/style or another drawing.
                arguments = getattr(tool_call, "arguments", {}) or {}
                if tool_name == "canvas" and arguments.get("action") == "compose":
                    try:
                        scene = json.loads(arguments.get("scene_json", ""))
                        identity = [scene.get("id"), scene.get("title")]
                        if all(isinstance(value, str) and value.strip() for value in identity):
                            refs["operation_key"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                    except (ValueError, TypeError, AttributeError):
                        pass
                entries.append(
                    EvidenceEntry(
                        tool_name=tool_name,
                        call_id=str(getattr(tool_call, "call_id", "") or ""),
                        status=(
                            "superseded"
                            if metadata.get("semantic_disposition")
                            == "superseded_neutral"
                            else "error"
                            if error
                            else (state_value or "completed")
                        ),
                        content_preview=content[:limit],
                        content_hash=digest,
                        output_path=str(
                            metadata.get("output_path")
                            or getattr(tool_call, "output_path", "")
                            or ""
                        ),
                        truncated=bool(
                            metadata.get("truncated")
                            or getattr(tool_call, "truncated", False)
                            or "[truncated]" in content.lower()
                            or len(content) > limit
                        ),
                        references=refs,
                    )
                )
        return cls(entries=entries)

    def has_tool_evidence(self) -> bool:
        return bool(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [entry.to_dict() for entry in self.entries]}

    def compact(self, *, max_entries: int = 20) -> dict[str, Any]:
        selected = self.entries[-max(1, max_entries) :]
        return {
            "entry_count": len(self.entries),
            "failed_count": sum(1 for entry in self.entries if entry.status == "error"),
            "truncated_count": sum(1 for entry in self.entries if entry.truncated),
            "entries": [entry.to_dict() for entry in selected],
        }

    def slice_for_contract(
        self,
        contract: Any,
        candidate: str = "",
        *,
        max_entries: int = 20,
        preview_chars: int = 1200,
    ) -> dict[str, Any]:
        """Select evidence relevant to the acceptance contract.

        The semantic validator must not receive a whole conversation or an
        arbitrary tail of tool output.  Ranking is deterministic and keeps
        source hashes/references so a decision can be audited.
        """

        contract_payload = (
            contract.to_dict()
            if hasattr(contract, "to_dict")
            else dict(contract or {})
            if isinstance(contract, dict)
            else {}
        )
        query_text = json.dumps(contract_payload, ensure_ascii=False, default=str)
        query_tokens = _evidence_tokens(query_text)
        candidate_tokens = _evidence_tokens(str(candidate or "")[:12_000])
        expected_paths: set[str] = set()
        for criterion in getattr(contract, "success_criteria", []) or []:
            for artifact in getattr(criterion, "expected_artifacts", []) or []:
                if isinstance(artifact, dict) and str(artifact.get("path") or "").strip():
                    expected_paths.add(str(artifact.get("path") or "").strip())

        ranked: list[tuple[float, int, EvidenceEntry]] = []
        for index, entry in enumerate(self.entries):
            serialized_refs = json.dumps(
                entry.references or {}, ensure_ascii=False, sort_keys=True, default=str
            )
            haystack = " ".join(
                (
                    entry.tool_name,
                    entry.call_id,
                    entry.content_preview,
                    entry.output_path,
                    serialized_refs,
                )
            )
            tokens = _evidence_tokens(haystack)
            contract_overlap = len(tokens & query_tokens) / max(1, len(query_tokens))
            candidate_overlap = len(tokens & candidate_tokens) / max(
                1, len(candidate_tokens)
            )
            path_match = 1.0 if entry.output_path in expected_paths else 0.0
            failure_weight = 0.15 if entry.status in {"error", "failed"} else 0.0
            score = contract_overlap * 0.55 + candidate_overlap * 0.2 + path_match + failure_weight
            if score > 0 or not query_tokens:
                ranked.append((score, index, entry))

        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        safe_limit = max(1, int(max_entries or 20))
        selected = ranked[:safe_limit]
        if not selected and self.entries:
            selected = [
                (0.0, index, entry)
                for index, entry in enumerate(self.entries[-safe_limit:])
            ]

        entries: list[dict[str, Any]] = []
        for score, _, entry in selected:
            payload = entry.to_dict()
            preview = str(payload.get("content_preview") or "")
            limit = max(preview_chars, 16_000) if entry.tool_name in {"intelligent_page", "canvas", "knowledge_search"} or entry.references.get("hypothesis_map") or entry.references.get("research_changes") else preview_chars
            if len(preview) > limit:
                payload["content_preview"] = (
                    preview[: max(0, limit - 24)] + " ...[truncated]"
                )
                payload["truncated"] = True
            payload["relevance_score"] = round(score, 5)
            entries.append(payload)
        return {
            "schema": "tcode.verifier-evidence-slice/v1",
            "entry_count": len(self.entries),
            "selected_count": len(entries),
            "failed_count": sum(1 for entry in self.entries if entry.status == "error"),
            "truncated_count": sum(1 for entry in self.entries if entry.truncated),
            "entries": entries,
        }
