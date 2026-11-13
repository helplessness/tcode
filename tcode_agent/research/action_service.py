"""Server-authoritative natural-language confirmation for research actions."""

from __future__ import annotations

import re
from typing import Any, Literal

from .action_models import ResearchPendingAction
from .action_store import ResearchActionStore
from .config import ResearchSettings, load_research_settings
from .pack_v2 import ResearchPackV2Builder
from .pack_v2_models import ResearchPackV2Scope
from .topics import ResearchTopicManager
from .workspace_service import (
    RESEARCH_WORKSPACE_SCHEMA,
    ResearchWorkspaceService,
)


ResearchActionDecision = Literal["confirm", "cancel", "unknown"]
_ACTION_MARKER = re.compile(
    r"<!--\s*tcode-research-action:([a-zA-Z0-9._:-]{8,180})\s*-->",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = re.compile(r"[\s。.!！?？,，;；:：]+$")


def research_action_marker(text: str) -> str:
    match = _ACTION_MARKER.search(str(text or ""))
    return str(match.group(1) if match else "").strip()


def interpret_research_action_decision(text: str) -> ResearchActionDecision:
    """Recognize a narrow explicit decision; ordinary conversational text is unknown."""

    cleaned = _ACTION_MARKER.sub("", str(text or "")).strip().casefold()
    if "?" in cleaned or "？" in cleaned:
        return "unknown"
    cleaned = _TRAILING_PUNCTUATION.sub("", cleaned).strip()
    structured = re.sub(r"\s+", "", cleaned)
    compact = re.sub(r"[\s,，。.!！:：;；'\"“”‘’()（）]+", "", cleaned)
    if not compact or len(compact) > 240:
        return "unknown"

    confirm_exact = {
        "确认",
        "确认执行",
        "确认继续",
        "确认生成",
        "确认生成researchpack",
        "确认设为正式课题",
        "确认设置为正式课题",
        "确认刚才的研究操作",
        "确认执行刚才的研究操作",
        "确定",
        "确定执行",
        "同意",
        "同意执行",
        "批准",
        "批准执行",
        "好的确认",
        "是确认",
        "yes",
    }
    cancel_exact = {
        "取消",
        "取消操作",
        "取消刚才的研究操作",
        "不确认",
        "不要执行",
        "先不要",
        "暂不执行",
        "放弃",
        "算了",
        "否",
        "no",
    }
    if compact in confirm_exact:
        return "confirm"
    if compact in cancel_exact:
        return "cancel"
    if re.fullmatch(r"确认(?:执行)?[《“].{1,160}[》”]这项研究操作吧?", structured):
        return "confirm"
    if re.fullmatch(r"取消[《“].{1,160}[》”]这项研究操作吧?", structured):
        return "cancel"
    return "unknown"


class ResearchActionCommitService:
    """Resolve a ticket in the current session and execute only explicit user intent."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str,
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        self.owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        if not all(self.owner.values()):
            raise ValueError("research action requires complete owner context")
        self.session_id = str(session_id or "").strip()
        if not self.session_id:
            raise ValueError("research action requires a session_id")
        self.workdir = str(workdir or "").strip() or None
        self.settings = settings or load_research_settings()

    def _record(self, action_id: str) -> ResearchPendingAction:
        normalized = str(action_id or "").strip()
        if normalized:
            record = ResearchActionStore.get(
                normalized,
                **self.owner,
                session_id=self.session_id,
                workdir=self.workdir,
                settings=self.settings,
            )
        else:
            record = ResearchActionStore.latest_pending(
                **self.owner,
                session_id=self.session_id,
                workdir=self.workdir,
                settings=self.settings,
            )
        if record is None:
            raise ValueError("当前会话没有等待确认的研究操作，请先让 Agent 重新展示操作预览。")
        return record

    @staticmethod
    def _presentation(record: ResearchPendingAction) -> dict[str, Any]:
        value = record.payload.get("presentation")
        return dict(value) if isinstance(value, dict) else {}

    def _update(
        self,
        record: ResearchPendingAction,
        *,
        status: str,
        decision_message_id: str,
        result: dict[str, Any],
    ) -> ResearchPendingAction:
        return ResearchActionStore.update_status(
            record.action_id,
            **self.owner,
            session_id=self.session_id,
            status=status,
            decision_message_id=decision_message_id,
            result=result,
            workdir=self.workdir,
            settings=self.settings,
        )

    def _cancel(
        self,
        record: ResearchPendingAction,
        *,
        decision_message_id: str,
    ) -> dict[str, Any]:
        action = self._presentation(record)
        action.update(
            {
                "action_id": record.action_id,
                "status": "cancelled",
                "message": "已取消，本次没有修改研究数据。",
            }
        )
        result = {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "commit",
            "status": "cancelled",
            "message": action["message"],
            "research_action": action,
        }
        self._update(
            record,
            status="cancelled",
            decision_message_id=decision_message_id,
            result=result,
        )
        return result

    def _blocked(
        self,
        record: ResearchPendingAction,
        *,
        decision_message_id: str,
        message: str,
        status: str = "failed",
    ) -> dict[str, Any]:
        action = self._presentation(record)
        action.update(
            {
                "action_id": record.action_id,
                "status": "blocked",
                "message": str(message or "研究操作未执行。"),
            }
        )
        result = {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "commit",
            "status": "blocked",
            "message": action["message"],
            "research_action": action,
        }
        self._update(
            record,
            status=status,
            decision_message_id=decision_message_id,
            result=result,
        )
        return result

    def _confirm_candidate(
        self,
        record: ResearchPendingAction,
        *,
        decision_message_id: str,
    ) -> dict[str, Any]:
        operation = dict(record.payload.get("operation") or {})
        try:
            candidate, project, _ = ResearchTopicManager(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).confirm(
                str(operation.get("candidate_id") or record.target_id),
                actor_id=self.owner["user_id"],
                expected_candidate_digest=str(
                    operation.get("candidate_digest") or record.target_digest
                ),
                reason="用户通过自然语言确认正式课题",
            )
        except ValueError as exc:
            message = str(exc)
            if "changed since preview" in message:
                self._update(
                    record,
                    status="stale",
                    decision_message_id=decision_message_id,
                    result={"error": message},
                )
                refreshed = ResearchWorkspaceService(
                    **self.owner,
                    session_id=self.session_id,
                    proposal_message_id=decision_message_id,
                    workdir=self.workdir,
                    settings=self.settings,
                ).propose_candidate_confirmation(record.target_id)
                refreshed["status"] = "stale"
                refreshed["message"] = "候选内容在确认前发生了变化，已生成新的确认预览。"
                refreshed["research_action"]["message"] = refreshed["message"]
                return refreshed
            return self._blocked(
                record,
                decision_message_id=decision_message_id,
                message=message,
            )

        action = self._presentation(record)
        action.update(
            {
                "action_id": record.action_id,
                "status": "completed",
                "message": "已确认为正式课题，来源记录与审计历史均已保留。",
                "confirmed_research_id": project.research_id,
                "candidate": {
                    **dict(action.get("candidate") or {}),
                    "state": candidate.state,
                    "confirmed_research_id": project.research_id,
                },
            }
        )
        result = {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "commit",
            "status": "completed",
            "message": action["message"],
            "project": ResearchWorkspaceService._project_summary(project),
            "research_action": action,
        }
        self._update(
            record,
            status="completed",
            decision_message_id=decision_message_id,
            result=result,
        )
        return result

    def _rename_candidate(
        self,
        record: ResearchPendingAction,
        *,
        decision_message_id: str,
    ) -> dict[str, Any]:
        operation = dict(record.payload.get("operation") or {})
        candidate_id = str(operation.get("candidate_id") or record.target_id).strip()
        proposed_title = str(operation.get("title") or "").strip()
        workspace = ResearchWorkspaceService(
            **self.owner,
            session_id=self.session_id,
            proposal_message_id=decision_message_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        detail = workspace.candidate_detail(candidate_id, page=1, page_size=5)
        expected_digest = str(
            operation.get("candidate_digest") or record.target_digest
        ).strip()
        if str(detail.get("candidate_digest") or "") != expected_digest:
            current = dict(detail.get("candidate") or {})
            if str(current.get("title") or "") == proposed_title:
                action = self._presentation(record)
                action.update(
                    {
                        "action_id": record.action_id,
                        "status": "completed",
                        "message": "候选课题已经使用该名称，无需重复修改。",
                        "candidate": current,
                    }
                )
                result = {
                    "schema": RESEARCH_WORKSPACE_SCHEMA,
                    "action": "commit",
                    "status": "completed",
                    "message": action["message"],
                    "candidate": current,
                    "research_action": action,
                }
                self._update(
                    record,
                    status="completed",
                    decision_message_id=decision_message_id,
                    result=result,
                )
                return result
            self._update(
                record,
                status="stale",
                decision_message_id=decision_message_id,
                result={"error": "candidate changed since preview"},
            )
            refreshed = workspace.propose_candidate_rename(
                candidate_id,
                title=proposed_title,
            )
            refreshed["status"] = "stale"
            refreshed["message"] = "候选内容在确认前发生了变化，已生成新的重命名预览。"
            refreshed["research_action"]["message"] = refreshed["message"]
            return refreshed
        try:
            candidate, _ = ResearchTopicManager(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).rename(
                candidate_id,
                title=proposed_title,
                actor_id=self.owner["user_id"],
                reason="用户通过自然语言重命名候选课题",
            )
        except ValueError as exc:
            return self._blocked(
                record,
                decision_message_id=decision_message_id,
                message=str(exc),
            )

        candidate_summary = ResearchWorkspaceService._candidate_summary(candidate)
        action = self._presentation(record)
        action.update(
            {
                "action_id": record.action_id,
                "status": "completed",
                "message": "候选课题已重命名，来源记录与审计历史均已保留。",
                "candidate": candidate_summary,
                "current_title": proposed_title,
            }
        )
        result = {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "commit",
            "status": "completed",
            "message": action["message"],
            "candidate": candidate_summary,
            "research_action": action,
        }
        self._update(
            record,
            status="completed",
            decision_message_id=decision_message_id,
            result=result,
        )
        return result

    def _dismiss_candidate(
        self,
        record: ResearchPendingAction,
        *,
        decision_message_id: str,
    ) -> dict[str, Any]:
        operation = dict(record.payload.get("operation") or {})
        candidate_id = str(operation.get("candidate_id") or record.target_id).strip()
        workspace = ResearchWorkspaceService(
            **self.owner,
            session_id=self.session_id,
            proposal_message_id=decision_message_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        detail = workspace.candidate_detail(candidate_id, page=1, page_size=5)
        current = dict(detail.get("candidate") or {})
        expected_digest = str(
            operation.get("candidate_digest") or record.target_digest
        ).strip()
        if str(detail.get("candidate_digest") or "") != expected_digest:
            if str(current.get("state") or "") == "dismissed":
                action = self._presentation(record)
                action.update(
                    {
                        "action_id": record.action_id,
                        "status": "completed",
                        "message": "该候选已经被忽略，无需重复操作。",
                        "candidate": current,
                    }
                )
                result = {
                    "schema": RESEARCH_WORKSPACE_SCHEMA,
                    "action": "commit",
                    "status": "completed",
                    "message": action["message"],
                    "candidate": current,
                    "research_action": action,
                }
                self._update(
                    record,
                    status="completed",
                    decision_message_id=decision_message_id,
                    result=result,
                )
                return result
            self._update(
                record,
                status="stale",
                decision_message_id=decision_message_id,
                result={"error": "candidate changed since preview"},
            )
            refreshed = workspace.propose_candidate_dismissal(candidate_id)
            refreshed["status"] = "stale"
            refreshed["message"] = "候选内容在确认前发生了变化，已生成新的忽略预览。"
            refreshed["research_action"]["message"] = refreshed["message"]
            return refreshed
        try:
            candidate, _ = ResearchTopicManager(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).dismiss(
                candidate_id,
                actor_id=self.owner["user_id"],
                reason="用户通过自然语言忽略候选课题",
            )
        except ValueError as exc:
            return self._blocked(
                record,
                decision_message_id=decision_message_id,
                message=str(exc),
            )

        candidate_summary = ResearchWorkspaceService._candidate_summary(candidate)
        action = self._presentation(record)
        action.update(
            {
                "action_id": record.action_id,
                "status": "completed",
                "message": "候选课题已忽略；原始 Session、文件和审计记录仍然保留。",
                "candidate": candidate_summary,
            }
        )
        result = {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "commit",
            "status": "completed",
            "message": action["message"],
            "candidate": candidate_summary,
            "research_action": action,
        }
        self._update(
            record,
            status="completed",
            decision_message_id=decision_message_id,
            result=result,
        )
        return result

    def _confirm_pack(
        self,
        record: ResearchPendingAction,
        *,
        decision_message_id: str,
    ) -> dict[str, Any]:
        operation = dict(record.payload.get("operation") or {})
        research_id = str(operation.get("research_id") or record.target_id).strip()
        privacy_profile = str(operation.get("privacy_profile") or "strict").strip()
        source_digest = str(operation.get("source_digest") or record.target_digest).strip()
        try:
            export = ResearchPackV2Builder(
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            ).build_scope(
                ResearchPackV2Scope(mode="project", research_ids=[research_id]),
                privacy_profile=privacy_profile,
                attribution_mode="anonymous",
                expected_source_digest=source_digest,
                acknowledge_privacy=True,
            )
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            if "digest" in message.casefold() or "changed" in message.casefold() or "变化" in message:
                self._update(
                    record,
                    status="stale",
                    decision_message_id=decision_message_id,
                    result={"error": message},
                )
                refreshed = ResearchWorkspaceService(
                    **self.owner,
                    session_id=self.session_id,
                    proposal_message_id=decision_message_id,
                    workdir=self.workdir,
                    settings=self.settings,
                ).preview_pack(
                    research_id=research_id,
                    privacy_profile=privacy_profile,
                )
                refreshed["status"] = "stale"
                refreshed["message"] = "研究内容在确认期间发生了变化，已刷新预览，请重新确认。"
                refreshed["research_action"]["message"] = refreshed["message"]
                return refreshed
            return self._blocked(
                record,
                decision_message_id=decision_message_id,
                message=message,
            )

        workspace = ResearchWorkspaceService(
            **self.owner,
            session_id=self.session_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        result = workspace._download_action(export)
        result["action"] = "commit"
        result["status"] = "completed"
        result["message"] = "ResearchPack 已生成，可以下载一次性副本。"
        result["research_action"]["action_id"] = record.action_id
        result["research_action"]["message"] = result["message"]
        self._update(
            record,
            status="completed",
            decision_message_id=decision_message_id,
            result=result,
        )
        return result

    def _confirm_research_step(self, record: ResearchPendingAction, *, decision_message_id: str) -> dict[str, Any]:
        from .experiment_store import ResearchExperimentStore
        operation = dict(record.payload.get("operation") or {})
        workspace = ResearchWorkspaceService(**self.owner, session_id=self.session_id,
                    proposal_message_id=decision_message_id, workdir=self.workdir, settings=self.settings)
        try:
            _, digest = workspace.research_step_preview(operation)
            if digest != record.target_digest:
                return self._blocked(record, decision_message_id=decision_message_id,
                    message="假说、实验或计算运行在预览后发生了变化，请重新生成预览并确认。", status="stale")
            owner = {**self.owner, "workdir": self.workdir}
            project = workspace._resolve_project(str(operation.get("research_id") or ""))
            step = operation["step"]
            if step == "create_hypothesis":
                item = ResearchExperimentStore.create_hypothesis(research_id=project.research_id,
                    statement=operation["statement"], rationale=operation.get("rationale", ""),
                    test_plan=operation.get("test_plan", ""), falsification_condition=operation.get("falsification_condition", ""),
                    created_by=self.owner["user_id"], **owner)
            elif step == "link_hypothesis_evidence":
                from .hypothesis_map import HypothesisMap
                from .knowledge_store import ResearchKnowledgeStore
                source = HypothesisMap(**owner).read_source(project.research_id, operation["object_type"], operation["object_id"])
                hypothesis = next(h for h in ResearchExperimentStore.list_hypotheses(project.research_id, **owner) if h.hypothesis_id == operation["hypothesis_id"])
                item, _ = ResearchKnowledgeStore.upsert_link(project.research_id, subject_type="hypothesis", subject_id=hypothesis.hypothesis_id,
                    object_type=operation["object_type"], object_id=operation["object_id"], relation=operation["relation"], discovered_by="agent",
                    metadata={"reason": operation["reason"].strip(), "scope": operation.get("scope", "").strip(), "review_status": "pending",
                        "hypothesis_revision": hypothesis.revision, "source_title": source["title"], "proposed_source_hash": source["content_hash"]}, **owner)
            elif step == "approve_hypothesis":
                item = ResearchExperimentStore.set_hypothesis_status(operation["hypothesis_id"], "approved", **owner)
            elif step == "create_experiment":
                item = ResearchExperimentStore.create_experiment(research_id=project.research_id,
                    hypothesis_id=operation["hypothesis_id"], title=operation["title"], reason=operation.get("reason", ""),
                    parent_experiment_id=operation.get("parent_experiment_id") or None,
                    execution_mode=operation.get("execution_mode", "shared_workspace"), created_by=self.owner["user_id"], **owner)
            else:
                old = ResearchExperimentStore.get_experiment(operation["experiment_id"], **owner)
                item = ResearchExperimentStore.update_experiment(old.experiment_id,
                    status="completed" if step == "complete_experiment" else old.status,
                    decision=operation.get("decision", old.decision) if step == "complete_experiment" else old.decision,
                    result_summary=operation.get("result_summary", old.result_summary) if step == "complete_experiment" else old.result_summary,
                    workflow_run_id=operation.get("workflow_run_id", old.workflow_run_id) if step == "attach_workflow" else old.workflow_run_id, **owner)
        except (ValueError, LookupError) as exc:
            return self._blocked(record, decision_message_id=decision_message_id, message=str(exc))
        action = self._presentation(record)
        action.update(action_id=record.action_id, status="completed", research_id=project.research_id,
                      message="研究操作已完成；本次没有提交、启动或重启计算。", result=item.to_dict())
        result = {"schema": RESEARCH_WORKSPACE_SCHEMA, "action": "commit", "status": "completed", "research_action": action}
        self._update(record, status="completed", decision_message_id=decision_message_id, result=result)
        return result

    def commit(
        self,
        *,
        action_id: str,
        user_text: str,
        decision_message_id: str,
    ) -> dict[str, Any]:
        marker = research_action_marker(user_text)
        requested = str(action_id or "").strip()
        if marker and requested and marker != requested:
            raise ValueError("研究操作引用不一致，未执行任何修改。")
        record = self._record(marker or requested)
        if record.status == "completed" and record.result:
            return dict(record.result)
        if record.status != "pending":
            raise ValueError(
                f"该研究操作当前状态为 {record.status}，不能再次执行；请重新生成预览。"
            )

        decision = interpret_research_action_decision(user_text)
        if decision == "unknown":
            action = self._presentation(record)
            action.update(
                {
                    "action_id": record.action_id,
                    "expires_at": record.expires_at,
                    "confirmation_hint": "请明确回复“确认”或“取消”。",
                }
            )
            return {
                "schema": RESEARCH_WORKSPACE_SCHEMA,
                "action": "commit",
                "status": "awaiting_confirmation",
                "message": "没有检测到明确决定；研究数据尚未修改。请回复“确认”或“取消”。",
                "research_action": action,
            }
        record = ResearchActionStore.claim_pending(
            record.action_id,
            **self.owner,
            session_id=self.session_id,
            decision_message_id=decision_message_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        try:
            if decision == "cancel":
                return self._cancel(record, decision_message_id=decision_message_id)
            if record.kind == "candidate_confirmation":
                return self._confirm_candidate(record, decision_message_id=decision_message_id)
            if record.kind == "candidate_rename":
                return self._rename_candidate(record, decision_message_id=decision_message_id)
            if record.kind == "candidate_dismissal":
                return self._dismiss_candidate(record, decision_message_id=decision_message_id)
            if record.kind == "pack_build":
                return self._confirm_pack(record, decision_message_id=decision_message_id)
            if record.kind == "research_step":
                return self._confirm_research_step(record, decision_message_id=decision_message_id)
            return self._blocked(
                record,
                decision_message_id=decision_message_id,
                message=f"暂不支持提交研究操作类型：{record.kind}",
            )
        except Exception as exc:
            try:
                self._update(
                    record,
                    status="failed",
                    decision_message_id=decision_message_id,
                    result={"error": str(exc)},
                )
            except Exception:
                pass
            if isinstance(exc, (RuntimeError, ValueError)):
                raise
            raise RuntimeError(str(exc)) from exc


__all__ = [
    "ResearchActionCommitService",
    "ResearchActionDecision",
    "interpret_research_action_decision",
    "research_action_marker",
]
