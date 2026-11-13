"""Workflow display and summary helpers used by the web server."""

import json
import re
from pathlib import Path
from typing import Any, Optional

from . import workflow_runtime
from ..workflow import (
    WorkflowCandidate,
    WorkflowProposal,
    WorkflowRun,
    WorkflowRunManager,
    WorkflowStageState,
    WorkflowStageTemplate,
    WorkflowTemplate,
)


def workflow_candidate_result_summary(candidates: list[WorkflowCandidate]) -> str:
    return workflow_runtime.workflow_candidate_result_summary(candidates)


def workflow_marker_summary(marker_type: str, payload: dict[str, Any]) -> str:
    return workflow_runtime.workflow_marker_summary(marker_type, payload)


def workflow_review_decision(text: str) -> str:
    return workflow_runtime.workflow_review_decision(text)


def workflow_review_revision_payload(
    stage: WorkflowStageTemplate,
    response: str,
) -> dict[str, Any]:
    return workflow_runtime.workflow_review_revision_payload(stage, response)


def auto_approve_workflow_loop_revision(
    run: WorkflowRun,
    template: WorkflowTemplate,
    *,
    stage_id: str,
    workdir: str,
) -> WorkflowRun:
    metadata = dict(template.metadata or {})
    if not metadata.get("loop_enabled") or not metadata.get("auto_approve_revision"):
        return run
    verification_stage_id = str(
        metadata.get("verification_stage_id") or "review_gate"
    ).strip()
    if str(stage_id or "").strip() != verification_stage_id:
        return run
    pending = [
        proposal
        for proposal in (run.proposals or [])
        if proposal.proposal_type == "revision"
        and proposal.status == "pending"
        and proposal.source_stage_id == verification_stage_id
    ]
    if not pending:
        return run
    requested_max = run.input_payload.get("max_loop_iterations")
    try:
        max_iterations = int(
            requested_max or metadata.get("max_loop_iterations", 3) or 3
        )
    except (TypeError, ValueError):
        max_iterations = 3
    max_iterations = max(1, min(20, max_iterations))
    return WorkflowRunManager.auto_approve_loop_revision(
        run.run_id,
        source_stage_id=verification_stage_id,
        max_iterations=max_iterations,
        workdir=workdir,
        expected_version=run.version,
    )


def workflow_async_stage_status(async_refs: list[dict[str, Any]]) -> str:
    return workflow_runtime.workflow_async_stage_status(async_refs)


def workflow_async_stage_summary(status: str, async_refs: list[dict[str, Any]]) -> str:
    return workflow_runtime.workflow_async_stage_summary(status, async_refs)


def _workflow_presentation(run: WorkflowRun) -> dict[str, Any]:
    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    value = metadata.get("presentation")
    return value if isinstance(value, dict) else {}


def _workflow_status_only(run: WorkflowRun) -> bool:
    return _workflow_presentation(run).get("stage_narration") == "status_only"


def _ontology_delivery_error(run: WorkflowRun) -> str:
    """Check persisted plugin references before exposing a completed ontology report."""
    if getattr(run, 'template_key', '') not in {'entity-guided-enzyme-discovery-demo', 'enzyme-entity-discovery-demo.entity-guided-enzyme-discovery-demo'}:
        return ''
    stage = _workflow_stage_by_id(run, 'critical_report')
    if stage is None or stage.status != 'committed':
        return ''
    import sqlite3
    import json
    import re
    from ..ontology_host import open_store
    from .state import ServerState
    from .workdirs import resolve_user_workdir_path
    payload = workflow_runtime.extract_json_payload_from_text(str((stage.committed_result or {}).get('response') or stage.result_summary or ''))
    payload = payload if isinstance(payload, dict) else {}
    private_ids = payload.get('private_assertion_ids') or []
    metadata = run.metadata or {}
    owner = tuple(str(metadata.get(key) or '') for key in ('tenant_id', 'user_id', 'workspace_id'))
    blocked = '本体报告暂不能交付：私有证据已撤回、不可访问或无法复核。请重新查询证据并生成报告。'
    if not all(owner):
        return blocked if private_ids else ''
    identifier = str(payload.get('run_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identifier):
        return blocked if private_ids else ''
    store = None
    try:
        root = resolve_user_workdir_path(ServerState.get_workdir(), owner[2], tenant_id=owner[0], user_id=owner[1])
        file = root / '.tcode/plugins/enzyme_entity_discovery_demo/runs' / identifier / '04_critical_report.json'
        if not file.resolve().is_relative_to(root) or any(p.is_symlink() for p in (file, *file.parents) if p != root and root in p.parents):
            return blocked
        if file.exists():
            document = json.loads(file.read_text())
            private_ids = sorted(set(private_ids) | set(document.get('private_assertion_ids') or []))
        elif private_ids:
            return blocked
        if private_ids:
            from ..workspace_lifecycle import assert_managed_workspace_creation_allowed
            assert_managed_workspace_creation_allowed(str(root), server_workdir=ServerState.get_workdir())
            store = open_store(ServerState.get_workdir(), owner)
            for start in range(0, len(private_ids), 20):
                store.validate_report([{'text': 'Workflow delivery references', 'assertion_ids': private_ids[start:start+20]}])
    except (ValueError, OSError, TypeError, KeyError, RuntimeError, sqlite3.Error):
        return blocked
    finally:
        if store is not None:
            store.close()
    return ''


def workflow_final_report_text(run: WorkflowRun) -> str:
    blocked = _ontology_delivery_error(run)
    if blocked:
        return blocked
    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    completion = _workflow_presentation(run).get("completion")
    if isinstance(completion, dict) and completion.get("source") == "stage_output_field":
        stage = _workflow_stage_by_id(run, str(completion.get("stage_id") or ""))
        if stage is None or stage.status != "committed":
            return ""
        # result_summary is deliberately truncated by the workflow model. Read
        # the validated, complete response so a display field never depends on
        # whether an internal receipt fits the short-summary budget.
        committed = stage.committed_result if isinstance(stage.committed_result, dict) else {}
        raw = committed.get("response") or stage.result_summary
        payload = workflow_runtime.extract_json_payload_from_text(str(raw or ""))
        value = payload.get(str(completion.get("field") or "")) if isinstance(payload, dict) else None
        if not isinstance(value, str):
            return ""
        from ..presentation.public_progress import scientific_user_text
        return scientific_user_text(value.strip(), request=workflow_original_user_request(run))

    direct = str(metadata.get("final_report", "") or "").strip()
    if direct:
        from ..presentation.public_progress import scientific_user_text
        return scientific_user_text(direct, request=workflow_original_user_request(run))

    proposals = run.proposals if isinstance(run.proposals, list) else []
    for proposal in reversed(proposals):
        if not isinstance(proposal, WorkflowProposal):
            continue
        proposal_type = str(getattr(proposal, "proposal_type", "") or "").strip().lower()
        proposal_status = str(getattr(proposal, "status", "") or "").strip().lower()
        if proposal_type != "final_report" or proposal_status != "approved":
            continue
        payload = proposal.payload if isinstance(proposal.payload, dict) else {}
        text = str(payload.get("final_report") or payload.get("summary") or payload.get("report") or "").strip()
        if text:
            from ..presentation.public_progress import scientific_user_text
            return scientific_user_text(text, request=workflow_original_user_request(run))

    for stage in run.stage_states or []:
        if str(getattr(stage, "stage_id", "") or "").strip() != "decision_report":
            continue
        text = str(getattr(stage, "result_summary", "") or "").strip()
        if text:
            return text
    return ""


def workflow_original_user_request(run: WorkflowRun) -> str:
    """Return the goal snapshot captured before the Workflow started."""

    metadata = dict(getattr(run, "metadata", {}) or {})
    intent_snapshot = dict(metadata.get("intent_snapshot") or {})
    candidates = [
        intent_snapshot.get("original_user_request"),
        metadata.get("auto_route_last_message"),
        dict(metadata.get("auto_route") or {}).get("input_payload", {}).get(
            "target_description"
        ),
        dict(getattr(run, "input_payload", {}) or {}).get("natural_language_request"),
        dict(getattr(run, "input_payload", {}) or {}).get("target_description"),
        dict(getattr(run, "input_payload", {}) or {}).get("design_goal"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:6000]
    return ""


def workflow_user_answer_text(
    run: WorkflowRun,
    final_report: str = "",
) -> str:
    """Project a Workflow-centric report into a user-question-centric answer."""

    blocked = _ontology_delivery_error(run)
    if blocked:
        return blocked

    metadata = dict(getattr(run, "metadata", {}) or {})
    answer_projection = dict(metadata.get("answer_projection") or {})
    composed = str(answer_projection.get("text") or "").strip()
    if composed:
        return composed

    report = str(final_report or workflow_final_report_text(run) or "").strip()
    if not report:
        return ""
    request = workflow_original_user_request(run)
    if not request:
        return (
            "所有步骤都已完成。以下是最终结果：\n\n"
            f"{report}"
        )

    compact_request = " ".join(request.split())
    if len(compact_request) > 360:
        compact_request = compact_request[:357].rstrip() + "..."
    return (
        f"针对你提出的“{compact_request}”，流程已经完成。"
        "以下是结合各阶段结果整理的回答：\n\n"
        f"{report}"
    )


def workflow_text_excerpt(text: str, *, max_chars: int = 6000) -> str:
    clean = str(text or "").strip()
    if len(clean) <= max_chars:
        return clean
    return clean[: max(0, max_chars - 44)].rstrip() + "\n\n...（内容已截断，完整结果见运行详情）"


_STAGE_FAILURE_REASON_LABELS: dict[str, str] = {
    "empty_stage_result": "这一步没有返回结果",
    "explicit_failure_signal": "这一步说明了无法继续的原因",
    "invalid_output_schema": "这一步返回的结果不完整，缺少必要信息",
    "expected_artifact_not_covered": "这一步没有说明它产出的结果文件",
    "missing_artifact": "这一步需要的结果文件没有生成",
    "artifact_file_missing": "这一步的结果文件没有成功保存",
    "required_output_text_missing": "这一步缺少必须提供的内容",
    "missing_review_decision": "这一步没有给出明确的处理结论",
    "dependency_not_committed": "上一步还没有完成，流程无法继续",
    "missing_stage_inputs": "缺少继续处理所需的信息",
    "evidence_store_unavailable": "核对结论所需的证据暂时不可用",
    "not_enough_evidence": "可核对的证据不足以支持结论",
    "missing_response_evidence_citation": "结论没有标注对应的证据来源",
    "unknown_evidence_refs": "结论引用了无法核对的证据来源",
    "required_evidence_entities_missing": "结论缺少必须覆盖的对象",
    "required_evidence_terms_missing": "结论缺少必须说明的内容",
    "unknown_validator": "流程配置需要调整",
}

_VALIDATION_SUMMARY_PREFIX_RE = re.compile(
    r"^(?:stage output rejected|output validation failed|阶段输出未通过)\s*[:：]\s*(?P<codes>.+)$",
    re.IGNORECASE,
)

_ARTIFACT_VALIDATOR_CODES = {
    "missing_artifact",
    "artifact_file_missing",
    "expected_artifact_not_covered",
}

# Known runtime messages that are still internal even though they are not
# validator codes. Keep the mapping minimal and explicit.
_INTERNAL_RUNTIME_REASONS: tuple[tuple[str, str], ...] = (
    (
        "output budget exhausted",
        "抱歉，后续处理遇到了一点问题，已有阶段结果已保留。",
    ),
    (
        "connection reset",
        "连接暂时中断。",
    ),
    (
        "workflow stage interrupted",
        "这一步因为系统中断没有完成，需要重新执行。",
    ),
    (
        "declared workflow tool receipt",
        "这一步的结果没有完整提交，需要重新执行。",
    ),
)

_INTERNAL_ERROR_HINT_RE = re.compile(
    r"(?:validator|json[-_]schema|stage_receipt|artifact(?:_refs)?|"
    r"lease expired|workflow (?:stage|run|worker|template|tool))",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _translate_validator_codes(code_text: str) -> str:
    codes: list[str] = []
    for chunk in re.split(r"[,，;；]+", str(code_text or "")):
        code = chunk.strip()
        if code and code not in codes:
            codes.append(code)
    reasons: list[str] = []
    if any(code in _ARTIFACT_VALIDATOR_CODES for code in codes):
        reasons.append("这一步没有生成需要的结果文件")
    for code in codes:
        if code in _ARTIFACT_VALIDATOR_CODES:
            continue
        label = _STAGE_FAILURE_REASON_LABELS.get(code)
        if label and label not in reasons:
            reasons.append(label)
    if not reasons:
        return "这一步没有按预期完成，流程没有继续。"
    return "、".join(reasons) + "。"


def workflow_user_facing_error_text(raw: str) -> str:
    """Hide internal validation and runtime errors behind plain language."""

    text = str(raw or "").strip()
    if not text:
        return ""

    prefix_match = _VALIDATION_SUMMARY_PREFIX_RE.match(text)
    if prefix_match is not None:
        return _translate_validator_codes(prefix_match.group("codes"))

    # Some persisted errors are already just a comma-separated code list.
    chunks = [
        chunk.strip() for chunk in re.split(r"[,，;；]+", text) if chunk.strip()
    ]
    if chunks and all(
        chunk in _STAGE_FAILURE_REASON_LABELS or chunk in _ARTIFACT_VALIDATOR_CODES
        for chunk in chunks
    ):
        return _translate_validator_codes(text)

    lowered = text.lower()
    for marker, label in _INTERNAL_RUNTIME_REASONS:
        if marker in lowered:
            return label
    if not _CJK_RE.search(text) and _INTERNAL_ERROR_HINT_RE.search(text):
        return "这一步没有按预期完成，流程没有继续。"
    return text


def workflow_user_facing_failure_reason(stage: Optional[WorkflowStageState]) -> str:
    """Translate a stage's internal failure into a user-facing reason."""

    if stage is None:
        return ""
    return workflow_user_facing_error_text(str(getattr(stage, "last_error", "") or ""))


def workflow_stage_status_label(status: str) -> str:
    normalized = str(status or "").strip().lower()
    labels = {
        "pending": "待执行",
        "queued": "排队中",
        "running": "执行中",
        "validating": "校验中",
        "committed": "已提交",
        "failed": "失败",
        "blocked": "已阻塞",
        "waiting_external_job": "等待外部任务",
        "review_pending": "等待审核",
        "collecting": "收集中",
        "cancelled": "已取消",
        "canceled": "已取消",
        "terminated": "已终止",
    }
    return labels.get(normalized, normalized or "未知")


def workflow_relevant_stage(run: WorkflowRun) -> Optional[WorkflowStageState]:
    current_stage_id = str(getattr(run, "current_stage_id", "") or "").strip()
    stages = list(getattr(run, "stage_states", []) or [])
    if current_stage_id:
        for stage in stages:
            if str(getattr(stage, "stage_id", "") or "") == current_stage_id:
                return stage

    for stage in reversed(stages):
        if str(getattr(stage, "status", "") or "").strip().lower() != "pending":
            return stage
    return None


def workflow_stage_display(stage: Optional[WorkflowStageState]) -> str:
    if stage is None:
        return ""
    stage_id = str(getattr(stage, "stage_id", "") or "").strip()
    label = str(getattr(stage, "label", "") or "").strip()
    status = workflow_stage_status_label(str(getattr(stage, "status", "") or ""))
    name = f"`{stage_id}`" if stage_id else "未知阶段"
    if label and label != stage_id:
        name += f"（{label}）"
    return f"{name}，{status}"


def workflow_recent_stage_summary_lines(run: WorkflowRun, *, limit: int = 5) -> list[str]:
    stages = [
        stage
        for stage in (getattr(run, "stage_states", []) or [])
        if str(getattr(stage, "status", "") or "").strip().lower() != "pending"
    ]
    lines: list[str] = []
    for stage in stages[-limit:]:
        stage_id = str(getattr(stage, "stage_id", "") or "").strip() or "unknown_stage"
        status = workflow_stage_status_label(str(getattr(stage, "status", "") or ""))
        summary = "" if _workflow_status_only(run) else str(getattr(stage, "result_summary", "") or "").strip()
        error = workflow_user_facing_failure_reason(stage)
        detail = "" if _workflow_status_only(run) else summary or error
        if detail:
            lines.append(f"- `{stage_id}`（{status}）：{workflow_text_excerpt(detail, max_chars=360)}")
        else:
            lines.append(f"- `{stage_id}`：{status}")
    return lines


def _workflow_stage_by_id(
    run: WorkflowRun,
    stage_id: str,
) -> Optional[WorkflowStageState]:
    normalized = str(stage_id or "").strip()
    if not normalized:
        return None
    return next(
        (
            stage
            for stage in (getattr(run, "stage_states", []) or [])
            if str(getattr(stage, "stage_id", "") or "").strip() == normalized
        ),
        None,
    )


def _workflow_stage_name(stage: Optional[WorkflowStageState]) -> str:
    if stage is None:
        return "当前步骤"
    return str(
        getattr(stage, "label", "")
        or getattr(stage, "stage_id", "")
        or "当前步骤"
    ).strip()


def _workflow_stage_position(run: WorkflowRun, stage_id: str) -> tuple[int, int]:
    stages = list(getattr(run, "stage_states", []) or [])
    normalized = str(stage_id or "").strip()
    for index, stage in enumerate(stages, start=1):
        if str(getattr(stage, "stage_id", "") or "").strip() == normalized:
            return index, len(stages)
    return 0, len(stages)


def _workflow_elapsed_text(elapsed_seconds: float) -> str:
    elapsed = max(0, int(elapsed_seconds or 0))
    if elapsed < 60:
        return f"约 {elapsed} 秒"
    minutes, seconds = divmod(elapsed, 60)
    if minutes < 60:
        return f"约 {minutes} 分钟" + (f" {seconds} 秒" if seconds >= 10 else "")
    hours, minutes = divmod(minutes, 60)
    return f"约 {hours} 小时" + (f" {minutes} 分钟" if minutes else "")


def _workflow_stage_goal(stage: WorkflowStageState) -> str:
    """Summarize the public stage contract without exposing its prompt."""

    metadata = dict(getattr(stage, "metadata", {}) or {})
    narration = metadata.get("narration")
    if isinstance(narration, dict):
        goal = str(narration.get("goal") or "").strip()
        if goal:
            return workflow_text_excerpt(goal, max_chars=320)
    public_actions = {
        "reaction_analysis": "对照反应物与产物，明确可观察的变化和目前还不能确定的部分",
        "mechanism_and_capability": "核对哪些机制能解释这些反应变化，区分已报道的能力、类比和尚缺证据的假说",
        "family_screening_and_routes": "比较具体候选对完整目标变化的支持与缺口，把单酶方向、局部步骤和未匹配能力分开",
        "critical_report": "复核候选的支持证据、反证与未知，形成保留验证边界的最终报告",
    }
    if stage.stage_id in public_actions:
        return public_actions[stage.stage_id]
    checks = [
        workflow_text_excerpt(str(item), max_chars=180)
        for item in (metadata.get("success_checks") or [])
        if str(item or "").strip()
    ]
    goal = "；".join(checks[:2])
    for internal, public in (("reported", "已报道"), ("Assertion", "结构化断言"), ("Evidence", "原文证据"), ("PASS", "检查通过")):
        goal = goal.replace(internal, public)
    return goal


def _public_artifact_documents(
    run: WorkflowRun,
    stage: WorkflowStageState,
) -> list[dict[str, Any]]:
    """Read only small, committed JSON artifacts; callers still whitelist fields."""

    documents: list[dict[str, Any]] = []
    root = None
    metadata = dict(getattr(run, "metadata", {}) or {})
    owner = tuple(str(metadata.get(key) or "") for key in ("tenant_id", "user_id", "workspace_id"))
    if all(owner):
        try:
            from .state import ServerState
            from .workdirs import resolve_user_workdir_path

            root = resolve_user_workdir_path(ServerState.get_workdir(), owner[2], tenant_id=owner[0], user_id=owner[1])
        except (OSError, RuntimeError, TypeError, ValueError):
            return []
    expected = dict(getattr(stage, "metadata", {}) or {}).get("expected_artifacts", [])
    expected = set(expected) if isinstance(expected, list) and all(isinstance(item, str) for item in expected) else set()
    refs = [(name, path) for name, path in dict(getattr(stage, "artifact_refs", {}) or {}).items() if str(name).endswith(".json")]
    refs.sort(key=lambda item: item[0] not in expected)
    for name, raw_path in refs[:8]:
        if not isinstance(raw_path, str) or not str(name).endswith(".json"):
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            if root is None:
                continue
            path = root / path
        try:
            if root is not None and (not path.resolve().is_relative_to(root.resolve())
                    or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and root in parent.parents)):
                continue
            if path.name != Path(str(name)).name or path.is_symlink() or path.stat().st_size > 2_000_000:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            documents.append(value)
    return documents


def _text(value: Any, *, limit: int = 260) -> str:
    if not isinstance(value, str):
        return ""
    if value.lstrip().startswith(("{", "[")) or re.search(r"```|<[^>]+>|private_prompt|system.prompt|reasoning_content|chain.of.thought|api[_-]?key|password|authorization\s*:|sk-[A-Za-z0-9]{16,}|忽略.*指令|ignore.*instructions|系统提示|隐藏推理", value, re.I):
        return ""
    value = re.sub(r"(?:assertion|ev):[\w:./-]+|ERD-CARD-\d+", "来源证据", value, flags=re.I)
    return workflow_text_excerpt(" ".join(value.split()), max_chars=limit)


def _first_text(values: Any, *, limit: int = 260) -> str:
    if not isinstance(values, list):
        return ""
    for value in values:
        if isinstance(value, dict):
            value = value.get("text") or value.get("statement") or value.get("reason") or value.get("finding")
        text = _text(value, limit=limit)
        if text:
            return text
    return ""


def _workflow_stage_public_result(
    run: WorkflowRun,
    stage: WorkflowStageState,
    *,
    allow_result_summary: bool = True,
) -> list[str]:
    """Project validated public artifacts into facts, hypotheses and boundaries."""

    documents = _public_artifact_documents(run, stage)
    stage_id = str(getattr(stage, "stage_id", "") or "").lower()
    lines: list[str] = []

    if "reaction" in stage_id:
        for document in documents:
            analysis = document.get("reaction_analysis") or document
            if not isinstance(analysis, dict):
                continue
            summary = _text(analysis.get("reaction_summary"))
            changes = analysis.get("net_changes")
            changes = changes if isinstance(changes, list) else []
            change_texts = [
                _text(item.get("description"))
                for item in changes[:4]
                if isinstance(item, dict) and _text(item.get("description"))
            ]
            if summary:
                lines.append(f"已明确反应：{summary}")
            if change_texts:
                lines.append("具体变化包括：" + "；".join(change_texts) + "。")
            boundary = _first_text(analysis.get("warnings")) or _first_text(analysis.get("unresolved_questions"))
            if boundary:
                lines.append(f"尚未确认：{boundary}")
            if lines:
                lines.append("对目标而言，这些变化必须被同一酶或可闭合的多步路线共同解释；只命中其中一项还不能算完整候选。")
                lines.append("以上是输入与文本证据支持的反应描述，不等同于实验验证。")
                return lines

    if "mechanism" in stage_id or "capability" in stage_id:
        for document in documents:
            hypotheses = document.get("mechanism_hypotheses") or document.get("hypotheses")
            if not isinstance(hypotheses, list):
                continue
            for item in hypotheses[:3]:
                if not isinstance(item, dict):
                    continue
                name = _text(item.get("title") or item.get("summary") or item.get("proposed_mechanism"), limit=180)
                detail = _text(item.get("proposed_mechanism") or item.get("summary"), limit=300)
                support = _first_text(item.get("supports") or item.get("fact_claims") or item.get("analogy_claims"))
                conflict = _first_text(item.get("contradictions") or item.get("counterarguments") or item.get("evidence_gaps"))
                if name:
                    sentence = f"候选假说“{name}”"
                    if detail and detail != name:
                        sentence += f"：{detail}"
                    if support:
                        sentence += f"；支持依据是{support}"
                    if conflict:
                        sentence += f"；仍有冲突或缺口：{conflict}"
                    lines.append(sentence + "。")
            gap = _first_text(document.get("global_knowledge_gaps") or document.get("knowledge_gaps"))
            if gap:
                lines.append(f"未验证边界：{gap}")
            if lines:
                lines.append("这些是待检验的机制解释，不能当作已证实的酶活性。")
                return lines

    if any(token in stage_id for token in ("screen", "candidate", "route")):
        for document in documents:
            candidates = document.get("entity_expanded_candidates") or document.get("enzyme_candidates")
            if not isinstance(candidates, list):
                continue
            ordered = sorted(candidates, key=lambda item: not (isinstance(item, dict) and item.get("candidate_role") == "primary"))
            for item in ordered[:3]:
                if not isinstance(item, dict):
                    continue
                name = _text(item.get("label") or item.get("enzyme_name") or item.get("enzyme_family"), limit=140)
                support = _text(item.get("fit_reason")) or _first_text(item.get("supports"))
                conflict = _first_text(item.get("conflicts") or item.get("contradictions") or item.get("knowledge_gaps"))
                if name:
                    role = {"primary": "单酶探索方向", "route_component": "多酶路线的局部组件", "diagnostic_control": "诊断对照", "family_hint": "家族线索", "unmatched_capability": "尚未匹配具体酶的催化能力"}.get(item.get("candidate_role"), "候选")
                    sentence = f"{role}“{name}”"
                    if support:
                        sentence += f"的入选依据是{support}"
                    if conflict:
                        sentence += f"；限制是{conflict}"
                    lines.append(sentence + "。")
            routes = document.get("routes")
            route = next((item for item in routes if isinstance(item, dict)), None) if isinstance(routes, list) else None
            if route and _text(route.get("title")):
                lines.append(f"另保留路线假说“{_text(route.get('title'))}”，其中未闭合步骤仍需验证。")
            if lines:
                lines.append("筛选的关键是区分能覆盖完整目标变化的方向和只承担局部步骤的组件，避免把局部反应相似误写成完整路线。")
                lines.append("候选排序表示证据与目标的匹配程度，不代表已经实验验证。")
                return lines

    for document in documents:
        decision = _text(document.get("review_decision"), limit=20).upper()
        critique = document.get("critique")
        critique = critique if isinstance(critique, dict) else document
        checks = document.get("checks")
        summary = _first_text(checks) or _text(document.get("user_summary"), limit=600)
        conflicts = _first_text(critique.get("hard_conflicts"))
        gap = _first_text(critique.get("evidence_gaps") or document.get("limitations"))
        revisions = _first_text(critique.get("required_revisions") or document.get("next_actions"), limit=400)
        if decision in {"PASS", "REVISE", "FAIL"}:
            lines.append("复核已通过。" if decision == "PASS" else "复核要求修订，当前结论不会作为已通过结果交付。")
            if decision == "PASS":
                lines.append("这里的通过表示结果表达与证据边界检查通过，不代表目标活性已经实验验证。")
        if summary:
            lines.append(summary)
        if conflicts:
            lines.append(f"审查发现的冲突：{conflicts}")
        if gap:
            lines.append(f"仍需保留的限制：{gap}")
        if revisions and decision != "PASS":
            lines.append(f"需要处理：{revisions}")
        if lines:
            return lines

    if not allow_result_summary:
        return []
    payload = workflow_runtime.extract_json_payload_from_text(str(getattr(stage, "result_summary", "") or ""))
    if isinstance(payload, dict):
        summary = _text(payload.get("public_summary") or payload.get("user_summary"), limit=900)
        return [summary] if summary else []
    summary = str(getattr(stage, "result_summary", "") or "").strip()
    if summary.startswith(("{", "[", "```")):
        return []
    clean = _text(summary, limit=900)
    return [clean] if clean else []


from ..presentation.public_progress import tool_public_progress as workflow_tool_public_progress


def workflow_run_narration_text(
    run: WorkflowRun,
    *,
    transient_update: str = "",
    sections: Optional[list[dict[str, Any]]] = None,
    commentary: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Render one durable, cumulative assistant reply for a Workflow run."""

    lines = ["## Workflow 进展"]
    stages = list(getattr(run, "stage_states", []) or [])
    current_stage_id = str(getattr(run, "current_stage_id", "") or "").strip()
    for index, stage in enumerate(stages, start=1):
        status = str(getattr(stage, "status", "") or "pending").strip().lower()
        if status == "pending" and str(getattr(stage, "stage_id", "") or "") != current_stage_id and not any(item.get("stage_id") == stage.stage_id for item in (sections or [])):
            continue
        label = _workflow_stage_name(stage)
        attempt = max(0, int(getattr(stage, "attempt", 0) or 0))
        revision = f" · 修订 {attempt}" if attempt > 1 else ""
        lines.extend(["", f"### {index}. {label} · {workflow_stage_status_label(status)}{revision}"])
        saved = [item for item in (sections or []) if item.get("stage_id") == stage.stage_id]
        updates = [item for item in (commentary or []) if item.get("stage_id") == stage.stage_id]
        for item in updates:
            prefix = "此前进展（已取代）：" if item.get("attempt") != attempt else ""
            lines.extend(["", "> " + prefix + str(item.get("text") or "")])
        for item in saved:
            if item.get("attempt") != attempt:
                lines.extend(["", f"此前版本（第 {item.get('attempt')} 次提交，已取代）：", str(item.get("text") or "")])
        if status == "committed":
            current = next((item for item in reversed(saved) if item.get("attempt") == attempt), None)
            if current and current.get("text"):
                lines.append(str(current["text"]))
                continue
            result = _workflow_stage_public_result(
                run,
                stage,
                allow_result_summary=not _workflow_status_only(run),
            )
            lines.extend(result or ["已提交，但公开产物中还没有足够内容可作可靠总结。"])
        elif status in {"running", "queued", "validating"}:
            goal = _workflow_stage_goal(stage)
            lines.append(f"正在核对：{goal}。" if goal else "正在处理，尚未产生可核对的阶段结果。")
        elif status in {"failed", "blocked"}:
            reason = workflow_user_facing_failure_reason(stage)
            lines.append(reason or "这一步尚未按预期完成，流程已暂停。")
        elif status == "waiting_external_job":
            lines.append("正在等待外部任务返回结果；目前没有新的可核对发现，收到结果后再继续。")
        elif status == "review_pending":
            lines.append("已进入审核等待，当前结果尚未获准作为最终结论交付。")
        elif status in {"cancelled", "canceled", "terminated"}:
            lines.append("这一步已停止，已有阶段结果保留；尚未完成的部分不作为结论。")

    update = str(transient_update or "").strip()
    if update:
        lines.extend(["", f"> {update}"])

    status = str(getattr(run, "status", "") or "").strip().lower()
    if status == "completed":
        report = workflow_final_report_text(run)
        lines.extend(["", "## 整体结论"])
        lines.append(report or "流程已经完成，但没有可安全展示的最终报告。")
    from ..presentation.public_progress import scientific_user_text
    return scientific_user_text(
        "\n".join(lines).strip(),
        request=workflow_original_user_request(run),
    )


def workflow_stage_running_narration_text(
    run: WorkflowRun,
    stage_id: str,
    *,
    elapsed_seconds: float = 0,
) -> str:
    """Build a concise start/heartbeat update for a running workflow stage."""

    stage = _workflow_stage_by_id(run, stage_id)
    if stage is None or str(getattr(stage, "status", "") or "").strip().lower() != "running":
        return ""
    position, total = _workflow_stage_position(run, stage_id)
    progress = f"（第 {position}/{total} 步）" if position and total else ""
    elapsed = max(0, float(elapsed_seconds or 0))
    if elapsed >= 15:
        text = (
            f"“{_workflow_stage_name(stage)}”仍在处理中{progress}，"
            f"已经运行{_workflow_elapsed_text(elapsed)}。\n\n"
            "目前还没有新的已提交结果，不需要你操作；有结果后我会马上同步。"
        )
        return text
    lines = [f"我开始处理“{_workflow_stage_name(stage)}”{progress}。"]
    goal = _workflow_stage_goal(stage)
    if goal:
        lines.extend(["", f"这一步会核对：{goal}。"])
    lines.extend(
        ["", (
            "这一步完成后会自动继续，结束时统一给出结论。"
            if _workflow_status_only(run)
            else "这一步完成后，我会先把结果摘要发到这里，再继续下一步。"
        )]
    )
    return "\n".join(lines).strip()


def workflow_stage_retry_narration_text(
    run: WorkflowRun,
    stage_id: str,
    *,
    retry_attempt: int,
    total_attempts: int,
    error_text: str = "",
) -> str:
    """Explain an automatic retry without exposing workflow implementation noise."""

    stage = _workflow_stage_by_id(run, stage_id)
    if stage is None:
        return ""
    current_attempt = max(2, int(retry_attempt or 0) + 1)
    attempts = max(current_attempt, int(total_attempts or 0))
    lines = [
        f"“{_workflow_stage_name(stage)}”刚才遇到一个临时问题，"
        f"我正在自动重试（第 {current_attempt}/{attempts} 次尝试）。",
    ]
    normalized_error = workflow_user_facing_error_text(str(error_text or ""))
    if normalized_error:
        lines.extend(["", f"刚才的问题：{workflow_text_excerpt(normalized_error, max_chars=500)}"])
    lines.extend(["", "现在不需要你重新提交；如果重试仍然失败，我会把原因和可选操作告诉你。"])
    return "\n".join(lines).strip()


def workflow_stage_narration_text(run: WorkflowRun, stage_id: str) -> str:
    """Build a conversational main-chat update for one committed stage."""

    stage = _workflow_stage_by_id(run, stage_id)
    if stage is None or str(getattr(stage, "status", "") or "").strip().lower() != "committed":
        return ""

    lines = [f"“{_workflow_stage_name(stage)}”已完成。"]
    public_result = _workflow_stage_public_result(
        run,
        stage,
        allow_result_summary=not _workflow_status_only(run),
    )
    lines.extend(["", *public_result] if public_result else ["", "这一步已经提交，但公开结果里还没有足够内容可作可靠解释；我不会用数量或内部回执冒充进展。"])

    status = str(getattr(run, "status", "") or "").strip().lower()
    if status in {"blocked", "failed", "error"}:
        lines.extend(
            [
                "",
                "这里还有需要确认或补充的内容，我先暂停继续推进，具体原因会在下一条消息中说明。",
            ]
        )
        from ..presentation.public_progress import scientific_user_text
        return scientific_user_text(
            "\n".join(lines).strip(),
            request=workflow_original_user_request(run),
        )

    next_stage = _workflow_stage_by_id(
        run,
        str(getattr(run, "current_stage_id", "") or "").strip(),
    )
    if next_stage is not None and str(getattr(next_stage, "status", "") or "").strip().lower() == "pending":
        next_goal = _workflow_stage_goal(next_stage)
        if next_goal:
            lines.extend(["", f"接下来会{next_goal}，以判断前面的结论能否继续保留。"])
        else:
            lines.extend(["", f"接下来我会继续“{_workflow_stage_name(next_stage)}”。"])
    elif status == "completed":
        lines.extend(["", "所有步骤都已完成，我正在整理最终结果。"])

    from ..presentation.public_progress import scientific_user_text
    return scientific_user_text(
        "\n".join(lines).strip(),
        request=workflow_original_user_request(run),
    )


def workflow_main_tab_message_text(run: WorkflowRun) -> str:
    run_id = str(getattr(run, "run_id", "") or "").strip()
    if not run_id:
        return ""

    status = str(getattr(run, "status", "") or "").strip().lower()
    status_label = workflow_stage_status_label(status)
    stage = workflow_relevant_stage(run)
    stage_display = workflow_stage_display(stage)
    stage_summary = str(getattr(stage, "result_summary", "") or "").strip() if stage else ""
    if _workflow_status_only(run):
        stage_summary = ""
    metadata = dict(getattr(run, "metadata", {}) or {})

    if status == "completed":
        report = workflow_final_report_text(run)
        if report:
            return workflow_user_answer_text(run, report)
        lines = ["所有步骤都已完成。"]
        summary_lines = workflow_recent_stage_summary_lines(run)
        if summary_lines:
            lines.extend(["", "阶段摘要：", *summary_lines])
        return "\n".join(lines).strip()

    if status in {"blocked", "failed", "error", "terminated", "cancelled", "canceled"}:
        lines = [f"处理暂时{status_label}。"]
        if stage_display:
            lines.append(f"停在：{stage_display}")
        reason = (
            workflow_user_facing_failure_reason(stage)
            or workflow_user_facing_error_text(str(metadata.get("termination_reason") or ""))
            or workflow_user_facing_error_text(str(metadata.get("cancel_reason") or ""))
        )
        if reason:
            lines.extend(["", f"原因：{workflow_text_excerpt(reason, max_chars=1200)}"])
        if stage and status in {"blocked", "failed", "error"}:
            from ..workflow.diagnostics import workflow_diagnostic
            diagnostic = workflow_diagnostic(stage.last_error, stage.validation_report, review_decision=str(stage.metadata.get("review_decision") or ""))
            # 使用结构化分类解释失败，不让模型根据原始异常猜测责任归属。
            lines = [f"处理暂时{status_label}。", f"问题类型：{diagnostic['title']}"]
            if stage_display:
                lines.append(f"停在：{stage_display}")
            lines.extend(["", diagnostic["message"], f"处理建议：{diagnostic['suggestion']}"])
            if reason:
                lines.append(f"具体情况：{workflow_text_excerpt(reason, max_chars=1200)}")
            completed = [s.label or s.stage_id for s in run.stage_states if s.committed_result or s.result_summary or s.artifact_refs]
            if completed or stage.draft_result:
                lines.append("已有结果已保留，可在运行详情查看；未通过校验的阶段输出保留为草稿。")
                if completed:
                    lines.append("可查看结果的阶段：" + "、".join(completed))
        if stage_summary and stage_summary != reason:
            lines.extend(["", f"阶段摘要：{workflow_text_excerpt(stage_summary, max_chars=1600)}"])
        lines.append("")
        lines.append("你可以直接在这里补充信息或给出调整意见；完整过程仍可在运行详情中查看。")
        return "\n".join(lines).strip()

    waiting_statuses = set(getattr(workflow_runtime, "WORKFLOW_WAITING_STAGE_STATUSES", set()) or set())
    if status in waiting_statuses:
        lines = ["当前步骤已经提交后台处理，现在正在等待结果或审核。"]
        if stage_display:
            lines.append(f"正在处理：{stage_display}")
        if stage_summary:
            lines.extend(["", f"目前进展：{workflow_text_excerpt(stage_summary, max_chars=1600)}"])
        lines.append("")
        lines.append("条件满足后我会继续下一步，并在这里同步结果。")
        return "\n".join(lines).strip()

    lines = [f"这一轮处理已经推进完成，当前状态：{status_label}。"]
    if stage_display:
        lines.append(f"接下来：{stage_display}")
    summary_lines = workflow_recent_stage_summary_lines(run)
    if summary_lines:
        lines.extend(["", "最近阶段摘要：", *summary_lines])
    lines.append("")
    lines.append("我会从这里继续处理；完整工具过程仍可在运行详情和 subagent 面板查看。")
    return "\n".join(lines).strip()


def workflow_parent_message_matches_run(message: Any, run_id: str) -> bool:
    if str(getattr(message, "role", "") or "") != "assistant":
        return False
    content = str(getattr(message, "content", "") or "")
    if not run_id or run_id not in content:
        return False
    markers = (
        "已按 workflow 接管这次任务",
        "已接管为 workflow 运行",
        "工作流",
    )
    return any(marker in content for marker in markers)
