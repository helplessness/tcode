"""Bounded, run-scoped evidence for Workflow completion acceptance.

Receipts prove execution/file delivery, not the scientific truth of model text.
Never load stage conversations, unrelated workspaces or the whole knowledge base.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .evidence_ledger import EvidenceEntry, EvidenceLedger

MAX_FILES = 8
MAX_FILE_BYTES = 256_000
MAX_EXCERPT_CHARS = 8_000
CHUNK_CHARS = 1_000


def workflow_completion_evidence(
    run: Any, workspace_root: str, expected_artifacts: list[dict[str, Any]]
) -> EvidenceLedger:
    run_id = str(run.run_id)
    stages = list(getattr(run, "stage_states", []) or [])
    receipt = {
        "run_id": run_id,
        "status": str(run.status),
        "stage_count": len(stages),
        "committed_count": sum(s.status == "committed" for s in stages),
        "stages": [
            {"stage_id": str(s.stage_id), "status": str(s.status),
             "attempt": int(getattr(s, "attempt", 0) or 0)}
            for s in stages[:32]
        ],
        "scope": "Execution receipt only; completion is not scientific validation.",
    }
    entries = [EvidenceEntry(
        tool_name="workflow.execution_receipt", call_id=run_id,
        content_preview=json.dumps(receipt, ensure_ascii=False)[:4_000],
        references={"run_id": run_id, "source": "persisted_workflow_state"},
    )]
    if not workspace_root:
        entries.append(EvidenceEntry(tool_name="workflow.artifact_receipt", status="error",
                                     content_preview="workspace_root unavailable; no files read"))
        return EvidenceLedger(entries)
    root = Path(workspace_root).resolve()
    paths = list(dict.fromkeys(str(a.get("path") or "") for a in expected_artifacts))
    # Prefer human-readable reports within the global excerpt budget.
    paths.sort(key=lambda p: (Path(p).suffix.lower() not in {".md", ".txt"}, p))
    remaining = MAX_EXCERPT_CHARS
    for ref in paths[:MAX_FILES]:
        facts: dict[str, Any] = {"run_id": run_id, "path": ref, "exists": False}
        body = ""
        digest = ""
        try:
            path = (root / ref).resolve()
            if not ref or not path.is_relative_to(root):
                raise ValueError("artifact outside workspace; not read")
            facts["exists"] = path.is_file()
            if not facts["exists"]:
                raise ValueError("artifact file missing")
            facts["size"] = path.stat().st_size
            if path.suffix.lower() in {".md", ".txt", ".json", ".mmd", ".csv"}:
                with path.open("rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) <= MAX_FILE_BYTES:
                    digest = hashlib.sha256(data).hexdigest()
                    body = data.decode("utf-8")
                else:
                    facts["content_omitted"] = "file exceeds bounded read budget"
            else:
                facts["content_omitted"] = "not an allowed text artifact"
        except (OSError, ValueError) as exc:
            facts["read_error"] = str(exc)[:240]
        entries.append(EvidenceEntry(
            tool_name="workflow.artifact_receipt", call_id=run_id,
            status="error" if facts.get("read_error") else "completed",
            content_preview=json.dumps(facts, ensure_ascii=False),
            output_path=ref, content_hash=digest, references=facts,
        ))
        budget = min(remaining, 5_000 if Path(ref).suffix.lower() in {".md", ".txt"} else 1_000)
        if not body or budget <= 0:
            continue
        # A bounded head/tail excerpt retains conclusions/limitations at the end.
        excerpt = body if len(body) <= budget else body[:budget // 2] + body[-budget // 2:]
        for start in range(0, len(excerpt), CHUNK_CHARS):
            entries.append(EvidenceEntry(
                tool_name="workflow.artifact_excerpt", call_id=run_id,
                content_preview=excerpt[start:start + CHUNK_CHARS],
                content_hash=digest, output_path=ref, truncated=len(body) > budget,
                references={"run_id": run_id, "path": ref,
                            "excerpt_part": start // CHUNK_CHARS + 1,
                            "selection": "head_tail" if len(body) > budget else "full",
                            "source": "artifact_content_not_independent_scientific_proof"},
            ))
        remaining -= len(excerpt)
    if len(paths) > MAX_FILES:
        entries.append(EvidenceEntry(tool_name="workflow.artifact_receipt", status="error",
                                    content_preview=f"{len(paths) - MAX_FILES} artifacts omitted by evidence budget"))
    return EvidenceLedger(entries)
