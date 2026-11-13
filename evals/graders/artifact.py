from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def grade_artifacts(
    task: dict[str, Any],
    artifacts: list[str],
    *,
    workdir: Path,
    output: str = "",
) -> dict[str, float]:
    expected = [str(item).strip() for item in task.get("expected_artifacts", []) if str(item).strip()]
    if not expected:
        return {}

    observed = {str(item) for item in artifacts or []}
    parsed_output = _extract_json(output)
    passed = 0
    for item in expected:
        path = Path(item)
        if not path.is_absolute():
            path = workdir / path
        if item in observed or path.exists() or _structured_artifact_present(item, parsed_output):
            passed += 1
    return {"artifact_pass": passed / len(expected)}


def _extract_json(text: str) -> Any:
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(value[start : end + 1])
        except Exception:
            return None
    return None


def _structured_artifact_present(name: str, parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    key = str(name or "").strip().lower()
    aliases = {
        "candidate_table": ["candidates", "candidate_table"],
        "evaluation_matrix": ["evaluation_matrix", "evaluations"],
        "final_report": ["final_report", "final_recommendation", "limitations"],
        "verification_decision": ["verifier", "verification_decision"],
    }
    for field in aliases.get(key, [key]):
        value = parsed.get(field)
        if value:
            return True
    return False
