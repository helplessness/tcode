"""Candidate registry and promotion ledger for protein run workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


_FASTA_SUFFIXES = {".fa", ".faa", ".fasta", ".fas", ".fsa"}
_PROMOTION_AGENTS = {
    "protein_orchestrator",
    "protein_verifier",
    "protein_report",
    "worker_verifier",
    "worker_report",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{id(payload)}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_id(value: str, *, fallback: str = "candidate") -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return (normalized[:80] or fallback).lower()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def candidate_index_path(run_root: str | Path) -> Path:
    return Path(run_root).expanduser().resolve(strict=False) / "candidates.json"


def _empty_index(run_root: Path) -> dict[str, Any]:
    return {
        "schema": "tcode.protein-candidates.v1",
        "run_root": run_root.as_posix(),
        "updated_at": _utc_now(),
        "candidates": [],
        "decisions": [],
    }


def load_candidate_index(run_root: str | Path) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve(strict=False)
    path = candidate_index_path(root)
    if not path.is_file():
        return _empty_index(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return _empty_index(root)
    if not isinstance(payload, dict):
        return _empty_index(root)
    payload["schema"] = "tcode.protein-candidates.v1"
    payload["run_root"] = root.as_posix()
    payload["candidates"] = [
        dict(item)
        for item in list(payload.get("candidates") or [])
        if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
    ]
    payload["decisions"] = [
        dict(item)
        for item in list(payload.get("decisions") or [])
        if isinstance(item, dict)
    ]
    return payload


def save_candidate_index(run_root: str | Path, index: dict[str, Any]) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve(strict=False)
    payload = dict(index or {})
    payload["schema"] = "tcode.protein-candidates.v1"
    payload["run_root"] = root.as_posix()
    payload["updated_at"] = _utc_now()
    payload["candidates"] = sorted(
        [dict(item) for item in list(payload.get("candidates") or []) if isinstance(item, dict)],
        key=lambda item: str(item.get("candidate_id") or ""),
    )
    payload["decisions"] = list(payload.get("decisions") or [])[-200:]
    _atomic_json(candidate_index_path(root), payload)
    _update_manifest(root, payload)
    return payload


def _update_manifest(run_root: Path, index: dict[str, Any]) -> None:
    manifest_path = run_root / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return
    if not isinstance(manifest, dict):
        return
    manifest["candidate_registry"] = candidate_manifest_summary(run_root, index)
    _atomic_json(manifest_path, manifest)


def candidate_manifest_summary(
    run_root: str | Path,
    index: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve(strict=False)
    payload = dict(index or load_candidate_index(root))
    candidates = list(payload.get("candidates") or [])
    decisions = list(payload.get("decisions") or [])
    return {
        "path": candidate_index_path(root).as_posix(),
        "candidate_count": len(candidates),
        "promoted_count": sum(
            1 for item in candidates if str(item.get("stage") or "") == "promoted"
        ),
        "candidates": candidates[:200],
        "candidates_truncated": len(candidates) > 200,
        "decisions": decisions[-200:],
    }


def _parse_fasta(path: Path) -> Iterable[tuple[str, str]]:
    header = ""
    sequence_parts: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith(">"):
                if header and sequence_parts:
                    yield header, "".join(sequence_parts)
                header = line[1:].strip() or path.stem
                sequence_parts = []
            elif header:
                sequence_parts.append(re.sub(r"\s+", "", line).upper())
    if header and sequence_parts:
        yield header, "".join(sequence_parts)


def _fasta_sources(
    run_root: Path,
    input_resources: list[dict[str, Any]],
) -> Iterable[tuple[Path, str, str, str]]:
    seen: set[str] = set()
    for item in input_resources:
        raw = str(item.get("path") or item.get("source_path") or "").strip()
        if not raw:
            continue
        source = Path(raw).expanduser().resolve(strict=False)
        candidates = [source] if source.is_file() else list(source.rglob("*"))[:512]
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in _FASTA_SUFFIXES:
                continue
            key = path.resolve(strict=False).as_posix()
            if key in seen:
                continue
            seen.add(key)
            yield (
                path,
                str(item.get("relative_path") or f"inputs/{path.name}"),
                str(item.get("container_path") or f"/workspace/inputs/{path.name}"),
                "input",
            )
    outputs_dir = run_root / "outputs"
    if outputs_dir.is_dir():
        for path in outputs_dir.rglob("*"):
            if (
                not path.is_file()
                or path.suffix.lower() not in _FASTA_SUFFIXES
                or _inside(path, outputs_dir / "promoted")
            ):
                continue
            key = path.resolve(strict=False).as_posix()
            if key in seen:
                continue
            seen.add(key)
            relative = path.relative_to(run_root).as_posix()
            yield path, relative, f"/workspace/{relative}", "generated"


def sync_candidate_index(
    run_root: str | Path,
    *,
    input_resources: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve(strict=False)
    if input_resources is None:
        input_resources = _manifest_inputs(root)
    index = load_candidate_index(root)
    existing = {
        str(item.get("candidate_id") or ""): dict(item)
        for item in list(index.get("candidates") or [])
        if isinstance(item, dict)
    }
    try:
        max_candidates = max(
            1,
            int(os.environ.get("TCODE_PROTEIN_RUN_MAX_CANDIDATES", "5000") or "5000"),
        )
    except (TypeError, ValueError):
        max_candidates = 5000
    discovered = 0
    for path, relative, container, origin in _fasta_sources(
        root,
        list(input_resources or []),
    ):
        try:
            artifact_sha = _sha256_file(path)
            for header, sequence in _parse_fasta(path):
                if discovered >= max_candidates:
                    break
                sequence_sha = hashlib.sha256(sequence.encode("utf-8")).hexdigest()
                candidate_id = f"{_safe_id(header.split()[0])}-{sequence_sha[:10]}"
                current = existing.get(candidate_id, {})
                artifacts = [
                    dict(item)
                    for item in list(current.get("artifacts") or [])
                    if isinstance(item, dict)
                ]
                artifact = {
                    "path": path.as_posix(),
                    "relative_path": relative,
                    "container_path": container,
                    "sha256": artifact_sha,
                    "origin": origin,
                }
                if not any(item.get("path") == artifact["path"] for item in artifacts):
                    artifacts.append(artifact)
                existing[candidate_id] = {
                    **current,
                    "candidate_id": candidate_id,
                    "name": str(current.get("name") or header),
                    "sequence_sha256": sequence_sha,
                    "sequence_length": len(sequence),
                    "stage": str(current.get("stage") or "discovered"),
                    "metrics": dict(current.get("metrics") or {}),
                    "evidence": list(current.get("evidence") or []),
                    "artifacts": artifacts,
                    "discovered_at": str(current.get("discovered_at") or _utc_now()),
                    "updated_at": _utc_now(),
                }
                discovered += 1
        except (OSError, UnicodeError, ValueError):
            continue
    index["candidates"] = list(existing.values())
    return save_candidate_index(root, index)


def _resolve_run_artifact(run_root: Path, raw_path: str) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("candidate artifact path is required")
    if raw == "/workspace":
        candidate = run_root
    elif raw.startswith("/workspace/"):
        candidate = run_root / raw[len("/workspace/") :]
    else:
        path = Path(raw).expanduser()
        candidate = path if path.is_absolute() else run_root / path
    candidate = candidate.resolve(strict=True)
    if not candidate.is_file():
        raise ValueError(f"candidate artifact is not a file: {raw}")
    if not (_inside(candidate, run_root / "inputs") or _inside(candidate, run_root / "outputs")):
        raise ValueError("candidate artifacts must be under protein run inputs/ or outputs/")
    return candidate


def register_candidate(
    run_root: str | Path,
    *,
    candidate_id: str,
    artifact_paths: list[str],
    metrics: Optional[dict[str, Any]] = None,
    evidence: Optional[list[str]] = None,
    notes: str = "",
    actor: str = "",
) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve(strict=False)
    index = sync_candidate_index(root, input_resources=_manifest_inputs(root))
    normalized_id = _safe_id(candidate_id)
    by_id = {
        str(item.get("candidate_id") or ""): dict(item)
        for item in list(index.get("candidates") or [])
        if isinstance(item, dict)
    }
    current = by_id.get(normalized_id, {})
    artifacts = [
        dict(item) for item in list(current.get("artifacts") or []) if isinstance(item, dict)
    ]
    for raw_path in artifact_paths:
        path = _resolve_run_artifact(root, raw_path)
        relative = path.relative_to(root).as_posix()
        record = {
            "path": path.as_posix(),
            "relative_path": relative,
            "container_path": f"/workspace/{relative}",
            "sha256": _sha256_file(path),
            "origin": "input" if relative.startswith("inputs/") else "generated",
        }
        if not any(item.get("path") == record["path"] for item in artifacts):
            artifacts.append(record)
    if not artifacts:
        raise ValueError("candidate registration requires at least one input/output artifact")
    normalized_metrics: dict[str, Any] = dict(current.get("metrics") or {})
    for key, value in dict(metrics or {}).items():
        name = str(key or "").strip()
        if name:
            normalized_metrics[name] = value
    candidate = {
        **current,
        "candidate_id": normalized_id,
        "name": str(current.get("name") or candidate_id).strip() or normalized_id,
        "stage": str(current.get("stage") or "registered"),
        "metrics": normalized_metrics,
        "evidence": list(dict.fromkeys([*list(current.get("evidence") or []), *list(evidence or [])])),
        "artifacts": artifacts,
        "notes": str(notes or current.get("notes") or "").strip(),
        "registered_by": str(actor or current.get("registered_by") or "").strip(),
        "registered_at": str(current.get("registered_at") or _utc_now()),
        "updated_at": _utc_now(),
    }
    by_id[normalized_id] = candidate
    index["candidates"] = list(by_id.values())
    save_candidate_index(root, index)
    return candidate


def compare_candidates(
    run_root: str | Path,
    *,
    metric: str = "",
    direction: str = "max",
    candidate_ids: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    root = Path(run_root).expanduser().resolve(strict=False)
    index = sync_candidate_index(root, input_resources=_manifest_inputs(root))
    selected = {
        _safe_id(item) for item in list(candidate_ids or []) if str(item or "").strip()
    }
    candidates = [
        dict(item)
        for item in list(index.get("candidates") or [])
        if isinstance(item, dict)
        and (not selected or str(item.get("candidate_id") or "") in selected)
    ]
    metric_name = str(metric or "").strip()
    if not metric_name:
        return candidates
    reverse = str(direction or "max").strip().lower() != "min"

    def _rank(item: dict[str, Any]) -> tuple[int, float]:
        value = dict(item.get("metrics") or {}).get(metric_name)
        try:
            return 1, float(value)
        except (TypeError, ValueError):
            return 0, float("-inf") if reverse else float("inf")

    return sorted(candidates, key=_rank, reverse=reverse)


def promote_candidate(
    run_root: str | Path,
    *,
    candidate_id: str,
    reason: str,
    evidence: list[str],
    review_verdict: str,
    actor: str,
) -> dict[str, Any]:
    root = Path(run_root).expanduser().resolve(strict=False)
    normalized_actor = str(actor or "").strip().lower()
    if normalized_actor not in _PROMOTION_AGENTS:
        raise PermissionError("candidate promotion requires verifier/report/orchestrator role")
    if str(review_verdict or "").strip().upper() != "PASS":
        raise ValueError("candidate promotion requires review_verdict=PASS")
    if not str(reason or "").strip() or not list(evidence or []):
        raise ValueError("candidate promotion requires a reason and evidence references")

    index = sync_candidate_index(root, input_resources=_manifest_inputs(root))
    normalized_id = _safe_id(candidate_id)
    candidates = [dict(item) for item in list(index.get("candidates") or [])]
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == normalized_id),
        None,
    )
    if candidate is None:
        raise KeyError(f"protein candidate not found: {normalized_id}")
    artifacts = [dict(item) for item in list(candidate.get("artifacts") or [])]
    if not artifacts:
        raise ValueError("candidate has no auditable artifacts")

    promoted_dir = root / "outputs" / "promoted" / normalized_id
    promoted_dir.mkdir(parents=True, exist_ok=True)
    promoted_artifacts: list[dict[str, Any]] = []
    expected_sequence_sha = str(candidate.get("sequence_sha256") or "").strip()
    selected_sequence = ""
    if expected_sequence_sha:
        for item in artifacts:
            source = Path(str(item.get("path") or "")).expanduser().resolve(strict=False)
            if not source.is_file() or source.suffix.lower() not in _FASTA_SUFFIXES:
                continue
            try:
                for _header, sequence in _parse_fasta(source):
                    if hashlib.sha256(sequence.encode("utf-8")).hexdigest() == expected_sequence_sha:
                        selected_sequence = sequence
                        break
            except (OSError, UnicodeError):
                continue
            if selected_sequence:
                break
    if selected_sequence:
        selected_fasta = promoted_dir / "candidate.fasta"
        wrapped = "\n".join(
            selected_sequence[index : index + 80]
            for index in range(0, len(selected_sequence), 80)
        )
        selected_fasta.write_text(
            f">{normalized_id}\n{wrapped}\n",
            encoding="utf-8",
        )
        promoted_artifacts.append(
            {
                "path": selected_fasta.as_posix(),
                "relative_path": selected_fasta.relative_to(root).as_posix(),
                "container_path": f"/workspace/{selected_fasta.relative_to(root).as_posix()}",
                "sha256": _sha256_file(selected_fasta),
                "sequence_sha256": expected_sequence_sha,
            }
        )
    for item in artifacts[:64]:
        source = Path(str(item.get("path") or "")).expanduser().resolve(strict=True)
        if not source.is_file():
            continue
        if selected_sequence and source.suffix.lower() in _FASTA_SUFFIXES:
            continue
        target = promoted_dir / source.name
        if target.exists() and target.resolve(strict=False) != source:
            target = promoted_dir / f"{source.stem}-{_sha256_file(source)[:10]}{source.suffix}"
        if source != target.resolve(strict=False):
            shutil.copy2(source, target)
        promoted_artifacts.append(
            {
                "path": target.as_posix(),
                "relative_path": target.relative_to(root).as_posix(),
                "container_path": f"/workspace/{target.relative_to(root).as_posix()}",
                "sha256": _sha256_file(target),
            }
        )
    if not promoted_artifacts:
        raise ValueError("candidate artifacts could not be materialized for promotion")

    decision = {
        "decision_id": hashlib.sha256(
            f"{normalized_id}:{_utc_now()}:{actor}".encode("utf-8")
        ).hexdigest()[:16],
        "action": "promote",
        "candidate_id": normalized_id,
        "reason": str(reason).strip(),
        "evidence": list(dict.fromkeys(str(item).strip() for item in evidence if str(item).strip())),
        "review_verdict": "PASS",
        "actor": str(actor or "").strip(),
        "timestamp": _utc_now(),
        "artifacts": promoted_artifacts,
        "biosafety": dict(_load_manifest(root).get("biosafety") or {}),
    }
    decision_path = promoted_dir / "decision.json"
    _atomic_json(decision_path, decision)
    candidate["stage"] = "promoted"
    candidate["promoted_at"] = decision["timestamp"]
    candidate["promotion_decision_id"] = decision["decision_id"]
    candidate["promoted_artifacts"] = promoted_artifacts
    candidate["updated_at"] = _utc_now()
    index["candidates"] = candidates
    index.setdefault("decisions", []).append(decision)
    save_candidate_index(root, index)
    return {"candidate": candidate, "decision": decision}


def _manifest_inputs(run_root: Path) -> list[dict[str, Any]]:
    payload = _load_manifest(run_root)
    return [
        dict(item)
        for item in list(payload.get("inputs") or [])
        if isinstance(item, dict)
    ] if isinstance(payload, dict) else []


def _load_manifest(run_root: Path) -> dict[str, Any]:
    path = run_root / "manifest.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}
