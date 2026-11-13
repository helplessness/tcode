"""Canonical candidates, session selections and existing research decision records."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DIRECTORY = Path(".tcode/plugins/enzyme_reaction_discovery/runs")
SOURCE = {"server_name": "enzyme_reaction_discovery", "app_id": "candidate_compare"}


class ResearchCompareBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    type: Literal["research_compare"]
    schema_id: Literal["tcode.research-compare.v1"] = Field(alias="schema")
    run_id: str = Field(min_length=1, max_length=128)
    result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=256)
    fallback: str = Field(max_length=512)


def normalize_compare(value):
    try:
        result = ResearchCompareBlock.model_validate(value).model_dump(by_alias=True)
        validate_run(result["run_id"])
        return result
    except ValueError:
        return None


def validate_run(run_id):
    if not isinstance(run_id, str) or not RUN.fullmatch(run_id) or ".." in run_id:
        raise ValueError("invalid_run_id")
    return run_id


def read_document(workdir, run_id, name):
    """Open each path component without following symlinks, bound before decoding."""
    validate_run(run_id)
    if name not in {"04_candidates.json", "run.json", "retrieval_screen.json", "retrieval_mechanism.json"}:
        raise ValueError("invalid_artifact")
    descriptor = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (*DIRECTORY.parts, run_id):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file_fd, "rb") as stream:
            import stat
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("invalid_artifact_file")
            raw = stream.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("artifact_too_large")
        def reject_constant(_value):
            raise ValueError("non_finite_artifact_number")
        value = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("invalid_artifact_document")
        return value, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(descriptor)


def load_result(workdir, run_id):
    document, digest = read_document(workdir, run_id, "04_candidates.json")
    candidates = document.get("enzyme_candidates")
    if document.get("run_id") != run_id or not isinstance(candidates, list) or not 1 <= len(candidates) <= 200:
        raise ValueError("invalid_candidate_result")
    ids = []
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get("candidate_id"), str) or not 1 <= len(item["candidate_id"]) <= 256:
            raise ValueError("invalid_candidate_identity")
        ids.append(item["candidate_id"])
        for key in ("enzyme_name", "candidate_kind", "candidate_role", "confidence", "evidence_scope", "verification_note", "native_reaction_quote"):
            if key in item and not isinstance(item[key], str):
                raise ValueError("invalid_candidate_fields")
        if item.get("score") is not None and (isinstance(item["score"], bool) or not isinstance(item["score"], (int, float))):
            raise ValueError("invalid_candidate_fields")
        for key in ("sources", "knowledge_gaps", "supports", "contradictions"):
            if key in item and not isinstance(item[key], list):
                raise ValueError("invalid_candidate_fields")
        for key in ("sources", "knowledge_gaps"):
            if any(not isinstance(v, str) for v in item.get(key, [])):
                raise ValueError("invalid_candidate_fields")
        for statement in [*item.get("supports", []), *item.get("contradictions", [])]:
            if not isinstance(statement, dict) or not isinstance(statement.get("source_refs", []), list) or any(not isinstance(v, str) for v in statement.get("source_refs", [])):
                raise ValueError("invalid_candidate_fields")
            if any(key in statement and not isinstance(statement[key], str) for key in ("statement", "kind")):
                raise ValueError("invalid_candidate_fields")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_candidate_identity")
    return {"run_id": run_id, "result_digest": digest, "candidates": candidates}


def compare_block(session_id, result):
    return {"type": "research_compare", "schema": "tcode.research-compare.v1", "session_id": session_id,
            "run_id": result["run_id"], "result_digest": result["result_digest"], "title": "候选比较与证据核对",
            "fallback": f"候选比较：{result['run_id']}，请使用支持研究比较台的客户端查看。"}


def list_runs(workdir):
    directory = Path(workdir) / DIRECTORY
    if any(path.is_symlink() for path in (Path(workdir), *(Path(workdir).joinpath(*DIRECTORY.parts[:i]) for i in range(1, len(DIRECTORY.parts) + 1)))):
        raise ValueError("unsafe_artifact_path")
    if not directory.exists():
        return []
    # Validate every document through the same confined reader; directories are never trusted.
    results = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name)[:200]:
        try:
            result = load_result(workdir, path.name)
            results.append({"run_id": path.name, "candidate_count": len(result["candidates"]), "result_digest": result["result_digest"]})
        except (OSError, ValueError):
            continue
    return results


def checked_result(workdir, run_id, digest):
    result = load_result(workdir, run_id)
    if digest != result["result_digest"]:
        raise ValueError("result_changed_reload_required")
    return result


def selection_for(session, run_id):
    from .result_selection import latest_selections
    return next((s for s in reversed(latest_selections(session.messages)) if s.get("source") == SOURCE and s["result_id"] == run_id), None)


def decision_owner(session):
    owner = {k: str(getattr(session, k, "") or "") for k in ("tenant_id", "user_id", "workspace_id")}
    if not all(owner.values()):
        raise ValueError("owner_context_required")
    return owner


def decision_for(session, run_id):
    from ..research.experience_store import ResearchExperienceStore
    validate_run(run_id)
    item = ResearchExperienceStore.get_active_by_key(f"intelligent-ui:enzyme:{run_id}",
        **decision_owner(session), workdir=session.workdir)
    if item is None:
        return None
    return decision_record(item)


def decision_record(item):
    ref = next((r for r in item.artifact_refs if r.get("type") == "enzyme_candidate_decisions"), {})
    return {"experience_id": item.experience_id, "revision": item.revision,
            "result_digest": ref.get("result_digest"), "decisions": ref.get("decisions", []),
            "research_id": next(iter(item.source_research_ids), ""), "occurred_at": item.occurred_at}


def decision_history(session, run_id, before_revision=0):
    from ..research.experience_store import ResearchExperienceStore
    validate_run(run_id)
    if not isinstance(before_revision, int) or before_revision < 0:
        raise ValueError("invalid_history_cursor")
    items = ResearchExperienceStore.list_items(**decision_owner(session), workdir=session.workdir,
        experience_key=f"intelligent-ui:enzyme:{run_id}", before_revision=before_revision,
        include_history=True, limit=51)
    return {"records": [decision_record(item) for item in items[:50]],
            "next_before_revision": items[49].revision if len(items) > 50 else 0}


def decision_state(session, digest, run_id):
    from ..research.config import load_research_settings
    from ..research.topic_store import ResearchTopicStore
    enabled = load_research_settings().experience_projection_active
    if not enabled:
        return {"enabled": False, "record": None, "projects": []}
    record = decision_for(session, run_id)
    return {"enabled": True, "record": record, "status": "stale" if record and record["result_digest"] != digest else "current",
        "projects": [{"research_id": p.research_id, "title": p.title} for p in ResearchTopicStore.list_projects(
            **decision_owner(session), workdir=session.workdir, limit=50)]}


def save_decisions(session, run_id, digest, decisions, research_id, expected_revision):
    from ..research.config import load_research_settings
    from ..research.experience_store import ResearchExperienceStore
    from ..research.topic_store import ResearchTopicStore
    from ..research.models import utc_now_iso
    if not load_research_settings().experience_projection_active:
        raise ValueError("research_decisions_disabled")
    owner = decision_owner(session)
    result = checked_result(session.workdir, run_id, digest)
    offered = {c["candidate_id"]: c for c in result["candidates"]}
    if not isinstance(decisions, list) or len(decisions) > 20:
        raise ValueError("invalid_candidate_decisions")
    ids = []
    for d in decisions:
        if (not isinstance(d, dict) or set(d) != {"candidate_id", "state", "reason"}
            or d["candidate_id"] not in offered or d["state"] not in {"adopt", "defer", "exclude"}
            or not isinstance(d["reason"], str) or not d["reason"].strip() or len(d["reason"]) > 500):
            raise ValueError("invalid_candidate_decision")
        ids.append(d["candidate_id"])
    if len(ids) != len(set(ids)) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError("invalid_decision_revision")
    if research_id:
        project = ResearchTopicStore.get_project(research_id, **owner, workdir=session.workdir)
        if project is None or project.status == "archived":
            raise ValueError("research_project_unavailable")
    normalized = sorted([{**d, "reason": d["reason"].strip()} for d in decisions], key=lambda d: d["candidate_id"])
    labels = {"adopt": "采用", "defer": "暂缓", "exclude": "排除"}
    summary = "用户研究决策，不代表实验验证或已启动计算。\n" + "\n".join(
        f"{d['candidate_id']}：{labels[d['state']]}。理由：{d['reason']}" for d in normalized)
    if len(summary) > 11000:
        raise ValueError("candidate_decisions_too_large")
    previous = decision_for(session, run_id)
    item, created = ResearchExperienceStore.append(**owner, workdir=session.workdir,
        kind="decision", title=f"候选研究决策 · {run_id}", summary=summary if normalized else "用户撤回本运行的全部候选决策；旧记录保留历史。",
        experience_key=f"intelligent-ui:enzyme:{run_id}", provenance="user_stated", outcome_status="not_validated",
        routing_state="resolved" if research_id else "unassigned", source_research_ids=[research_id] if research_id else [],
        artifact_refs=[{"type": "enzyme_candidate_decisions", "run_id": run_id, "result_digest": digest,
                        "session_id": session.id, "decisions": normalized,
                        "path": str(DIRECTORY / run_id / "04_candidates.json")}],
        occurred_at=previous["occurred_at"] if previous else utc_now_iso(), expected_revision=expected_revision)
    return {"experience_id": item.experience_id, "revision": item.revision, "created": created,
            "result_digest": digest, "decisions": normalized, "research_id": research_id, "occurred_at": item.occurred_at}


def save_selection(session, run_id, digest, ids, reason):
    from .result_selection import normalize_selection
    from ..types import Message
    import time
    import uuid
    result = checked_result(session.workdir, run_id, digest)
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ValueError("invalid_selection_reason")
    offered = {c["candidate_id"]: c for c in result["candidates"]}
    if not 1 <= len(ids) <= 20 or len(ids) != len(set(ids)) or any(i not in offered for i in ids):
        raise ValueError("invalid_candidate_selection")
    items = [{"item_id": i, "item_type": "enzyme_candidate", "label": str(offered[i].get("enzyme_name") or i)[:256],
              "metrics": {k: offered[i].get(k) for k in ("score", "confidence", "evidence_scope", "verification_note")}}
             for i in sorted(ids)]
    value = {"type": "result_selection", "schema": "tcode.result-selection.v1", "result_id": run_id,
             "result_digest": digest, "items": items, "source": SOURCE, "inputs": {"reason": reason},
             "producer": {"plugin": "enzyme_reaction_discovery"}}
    value["selection_key"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    value = normalize_selection(value)
    if value is None:
        raise ValueError("selection_exceeds_context_limit")
    previous = selection_for(session, run_id)
    if previous and previous["selection_key"] == value["selection_key"]:
        return previous
    value.update(selected_at=time.time_ns(), event_id="selection-" + uuid.uuid4().hex)
    session.messages.append(Message(role="system", content="用户选择研究候选，详情见结构化数据；不代表已验证或已采用。",
        id=value["event_id"], content_blocks=[value], metadata={"event_type": "result_selection", "platform_event": True, "selected_at": value["selected_at"]}))
    return value


def validate_selections(selections, workdir, session=None):
    values = deepcopy(selections)
    for selection in values:
        if selection.get("source") == SOURCE:
            try:
                checked_result(workdir, selection["result_id"], selection.get("result_digest"))
                selection["status"] = "current"
                if session is not None:
                    from ..research.config import load_research_settings
                    if load_research_settings().experience_projection_active:
                        decision = decision_for(session, selection["result_id"])
                        if decision:
                            selection["research_decisions"] = {**decision, "status": "current" if decision["result_digest"] == selection["result_digest"] else "stale_do_not_apply"}
            except (OSError, ValueError):
                selection["status"] = "stale_or_unavailable"
                selection["warning"] = "结果已改变或不可读取；旧选择仅作历史，不得用于当前判断，请重新打开并选择。"
    return values


def read_evidence(workdir, run_id, digest, candidate_id, reference):
    result = checked_result(workdir, run_id, digest)
    candidate = next((c for c in result["candidates"] if c["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise ValueError("candidate_not_found")
    refs = set(candidate.get("sources") or [])
    for statement in [*(candidate.get("supports") or []), *(candidate.get("contradictions") or [])]:
        if isinstance(statement, dict):
            refs.update(statement.get("source_refs") or [])
    if reference not in refs:
        raise ValueError("evidence_not_bound_to_candidate")
    if not reference.startswith("ERD-CARD-"):
        return {"status": "unverified", "reference": reference, "message": "模型提议或未定位来源；没有可核对的原文片段。"}
    run, _ = read_document(workdir, run_id, "run.json")
    for name in ("retrieval_screen.json", "retrieval_mechanism.json"):
        try:
            snapshot, snapshot_digest = read_document(workdir, run_id, name)
            claims = snapshot.get("evidence_snapshots", {})
            claim = claims.get(reference) if isinstance(claims, dict) else None
            if (run.get("knowledge_fingerprint") and snapshot.get("knowledge_fingerprint") == run["knowledge_fingerprint"]
                and isinstance(claim, dict) and claim.get("ref") == reference and isinstance(claim.get("statement"), str)):
                return {"status": "archived_snapshot", "reference": reference, "claim": claim,
                        "snapshot_digest": snapshot_digest, "knowledge_fingerprint": run["knowledge_fingerprint"],
                        "message": "这是该运行保存的证据快照；未重新核验当前知识库或远程论文。既往反应不能直接证明目标活性。"}
        except (OSError, ValueError):
            continue
    try:
        from plugins.enzyme_reaction_discovery.tools.knowledge_tools import KnowledgeLibrary
        from plugins.enzyme_reaction_discovery.tools.wiki_tools import wiki_packet
        plugin = Path(__file__).resolve().parents[2] / "plugins/enzyme_reaction_discovery"

        class WikiReader:
            def resolve_read_path(self, path):
                target = Path(path).resolve(strict=True)
                if not any(target.is_relative_to(plugin / prefix) for prefix in ("knowledge", "schemas")):
                    raise PermissionError("wiki_path_outside_library")
                return str(target)

            def read_text(self, path, encoding="utf-8"):
                return Path(self.resolve_read_path(path)).read_text(encoding=encoding)

        packet = wiki_packet(KnowledgeLibrary(WikiReader(), plugin), run, refs=[reference])
        claim = packet["cards"][0]["claims"][0]
        return {"status": "available", "reference": reference, "claim": claim, "scope": packet["scope"]}
    except (ImportError, OSError, ValueError, KeyError, PermissionError):
        return {"status": "unavailable_or_stale", "reference": reference, "message": "原文片段不可用或知识库版本已变化；不能把旧引用当作当前已核实证据。"}
