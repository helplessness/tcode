"""Owner-scoped control-plane reads and safe action proposals for research workspaces."""

from __future__ import annotations

import math
import re
from typing import Any

from .action_store import ResearchActionStore
from .config import ResearchSettings, load_research_settings
from .models import ResearchEvent
from .pack_store import ResearchPackStore
from .pack_v2 import ResearchPackV2Builder
from .pack_v2_models import ResearchPackV2Scope
from .topic_models import ResearchProject, ResearchTopicCandidate, research_candidate_digest
from .topic_store import ResearchTopicStore
from .experiment_store import ResearchExperimentStore
from .models import canonical_json, sha256_text


RESEARCH_WORKSPACE_SCHEMA = "tcode.research-workspace.v1"
RESEARCH_ACTION_SCHEMA = "tcode.research-action.v1"
_SOURCE_CATEGORIES = ("message", "tool", "mcp", "artifact", "workflow", "agent", "file", "other")
_RESEARCH_STEP_LABELS = {"link_hypothesis_evidence": "关联假说与证据","create_hypothesis": "提出假说", "approve_hypothesis": "批准假说",
    "create_experiment": "创建实验记录 / 尝试另一条路线", "complete_experiment": "完成并冻结实验", "attach_workflow": "关联已有计算运行"}
_TECHNICAL_EVENT_TYPES = {
    "session.created",
    "session.archived",
    "session.resumed",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "message.compacted",
    "agent.spawned",
}
_TECHNICAL_REFERENCE = re.compile(
    r"(?i)(?:^|\s)(?:session|trace|event|turn|call|episode|checkpoint|run)"
    r"\s*[:#-]\s*[a-z0-9._:-]+"
)


def _event_category(event: ResearchEvent) -> str:
    event_type = str(event.event_type or "").lower()
    source_kind = str(event.source_kind or "").lower()
    if event_type.startswith("message.") or source_kind == "session_message":
        return "message"
    if event_type.startswith("mcp.") or source_kind == "mcp_tool":
        return "mcp"
    if event_type.startswith("workflow.") or source_kind == "workflow":
        return "workflow"
    if event_type.startswith("agent.") or source_kind == "subagent":
        return "agent"
    if event_type.startswith(("artifact.", "document.")) or source_kind in {
        "artifact",
        "tool_artifact",
        "document",
    }:
        return "artifact"
    if event_type.startswith("file.") or source_kind in {"file_upload", "file_change"}:
        return "file"
    if event_type.startswith("tool.") or source_kind == "tool_call":
        return "tool"
    return "other"


def _readable_payload(event: ResearchEvent) -> str:
    payload = dict(event.payload or {})
    value = next(
        (
            payload.get(key)
            for key in (
                "content_preview",
                "output_preview",
                "error_preview",
                "paper_title",
                "document_title",
                "artifact_title",
                "display_name",
                "summary_text",
                "summary",
                "statement",
                "description",
                "task",
                "filename",
            )
            if str(payload.get(key) or "").strip()
        ),
        "",
    )
    lines = [
        line.strip()
        for line in str(value or "").splitlines()
        if line.strip() and not line.lstrip().startswith("# Task Contract")
    ]
    text = _TECHNICAL_REFERENCE.sub(" ", " ".join(lines))
    return re.sub(r"\s+", " ", text).strip(" \t\r\n-—:：")[:500]


def _is_technical_event(event: ResearchEvent) -> bool:
    event_type = str(event.event_type or "").lower()
    category = _event_category(event)
    readable = _readable_payload(event)
    if event_type in _TECHNICAL_EVENT_TYPES or event_type.endswith(".started"):
        return True
    if event_type == "message.created" and not readable:
        return True
    if category in {"tool", "mcp", "workflow", "agent", "artifact"} and not readable:
        return True
    return False


def _source_title(event: ResearchEvent, category: str) -> str:
    if category == "message":
        return "用户研究请求" if event.actor_type == "user" else "Agent 研究记录"
    if category == "file":
        return "上传研究文件" if event.event_type == "file.uploaded" else "更新研究文件"
    if category == "artifact":
        return "生成研究产物"
    if category == "agent":
        return "子 Agent 研究结果"
    if category == "workflow":
        return "研究工作流结果"
    if category == "mcp":
        return "MCP 执行失败" if event.event_type.endswith("failed") else "MCP 执行结果"
    if category == "tool":
        return "工具执行失败" if event.event_type.endswith("failed") else "工具执行结果"
    return "研究记录"


def _page(items: list[Any], *, page: int, page_size: int) -> tuple[list[Any], dict[str, int]]:
    safe_page = max(1, int(page or 1))
    safe_size = max(1, min(int(page_size or 10), 20))
    total = len(items)
    pages = max(1, math.ceil(total / safe_size))
    safe_page = min(safe_page, pages)
    start = (safe_page - 1) * safe_size
    return items[start : start + safe_size], {
        "page": safe_page,
        "page_size": safe_size,
        "total": total,
        "pages": pages,
    }


class ResearchWorkspaceService:
    """Provide bounded research management views without trusting model-owned scope."""

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        session_id: str = "",
        proposal_message_id: str = "",
        workdir: str | None = None,
        settings: ResearchSettings | None = None,
    ) -> None:
        self.owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        if not all(self.owner.values()):
            raise ValueError("research workspace requires complete owner context")
        self.session_id = str(session_id or "").strip()
        self.proposal_message_id = str(proposal_message_id or "").strip()
        self.workdir = str(workdir or "").strip() or None
        self.settings = settings or load_research_settings()

    def _attach_pending_action(
        self,
        action: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
        target_digest: str,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach a durable ticket only to executable, chat-originated proposals."""

        if action.get("status") != "ready" or not self.session_id:
            return action
        record = ResearchActionStore.create_pending(
            **self.owner,
            session_id=self.session_id,
            proposal_message_id=self.proposal_message_id,
            kind=str(action.get("kind") or ""),
            target_type=target_type,
            target_id=target_id,
            target_digest=target_digest,
            payload={
                "operation": dict(operation),
                "presentation": dict(action),
            },
            workdir=self.workdir,
            settings=self.settings,
        )
        action["action_id"] = record.action_id
        action["expires_at"] = record.expires_at
        action["confirmation_hint"] = "请直接回复“确认”执行，或回复“取消”放弃。"
        return action

    def _projects(self, *, include_archived: bool = True) -> list[ResearchProject]:
        return ResearchTopicStore.list_projects(
            **self.owner,
            include_archived=include_archived,
            limit=1000,
            workdir=self.workdir,
            settings=self.settings,
        )

    def _candidates(self, *, include_inactive: bool = False) -> list[ResearchTopicCandidate]:
        return ResearchTopicStore.list_candidates(
            **self.owner,
            states=[] if include_inactive else ["emerging", "stable"],
            include_inactive=include_inactive,
            limit=1000,
            workdir=self.workdir,
            settings=self.settings,
        )

    def _project(self, research_id: str) -> ResearchProject:
        project = ResearchTopicStore.get_project(
            str(research_id or "").strip(),
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if project is None:
            raise ValueError("research project was not found in the current workspace")
        return project

    def _candidate(self, candidate_id: str) -> ResearchTopicCandidate:
        candidate = ResearchTopicStore.get_candidate(
            str(candidate_id or "").strip(),
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        )
        if candidate is None:
            raise ValueError("research candidate was not found in the current workspace")
        return candidate

    def _resolve_project(self, research_id: str = "", query: str = "") -> ResearchProject:
        if str(research_id or "").strip():
            return self._project(research_id)
        active = [item for item in self._projects(include_archived=False) if item.status != "archived"]
        normalized_query = str(query or "").strip().casefold()
        if normalized_query:
            matching = [
                item
                for item in active
                if normalized_query in item.title.casefold()
                or normalized_query in item.description.casefold()
            ]
            if len(matching) == 1:
                return matching[0]
            if len(matching) > 1:
                raise ValueError("multiple research projects match; ask the user to choose one")
        if len(active) == 1:
            return active[0]
        if not active:
            raise ValueError("the current workspace has no formal research project")
        raise ValueError("multiple research projects are active; ask the user to choose one")

    @staticmethod
    def _candidate_summary(candidate: ResearchTopicCandidate) -> dict[str, Any]:
        return {
            "candidate_id": candidate.candidate_id,
            "title": candidate.suggested_title,
            "title_source": candidate.title_source,
            "goal_summary": candidate.goal_summary,
            "keywords": candidate.keywords[:8],
            "state": candidate.state,
            "confirmed_research_id": candidate.confirmed_research_id,
            "stability_score": candidate.stability_score,
            "supporting_event_count": candidate.supporting_event_count,
            "session_count": candidate.session_count,
            "tool_count": candidate.tool_count,
            "artifact_count": candidate.artifact_count,
            "last_seen_at": candidate.last_seen_at,
        }

    @staticmethod
    def _project_summary(project: ResearchProject) -> dict[str, Any]:
        return {
            "research_id": project.research_id,
            "title": project.title,
            "description": project.description,
            "status": project.status,
            "updated_at": project.updated_at,
            "archived_at": project.archived_at,
        }

    def overview(self) -> dict[str, Any]:
        projects = self._projects(include_archived=True)
        candidates = self._candidates(include_inactive=False)
        exports = ResearchPackStore.list_exports(
            **self.owner,
            limit=200,
            workdir=self.workdir,
            settings=self.settings,
        )
        project_counts = {"active": 0, "paused": 0, "archived": 0}
        for item in projects:
            project_counts[item.status] = project_counts.get(item.status, 0) + 1
        completed = [item for item in exports if item.status == "completed"]
        pending = self.pending_action() if self.session_id else None
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "overview",
            "workspace_id": self.owner["workspace_id"],
            "formal_project_count": len([item for item in projects if item.status != "archived"]),
            "project_counts": project_counts,
            "candidate_count": len(candidates),
            "research_pack_count": len(exports),
            "download_ready_pack_count": len(completed),
            "projects": [self._project_summary(item) for item in projects[:10]],
            "candidates": [self._candidate_summary(item) for item in candidates[:10]],
            "pending_action": pending.get("pending_action") if pending else None,
        }

    def pending_action(self) -> dict[str, Any]:
        if not self.session_id:
            return {
                "schema": RESEARCH_WORKSPACE_SCHEMA,
                "action": "pending_action",
                "status": "none",
                "message": "当前调用没有会话上下文。",
                "pending_action": None,
            }
        record = ResearchActionStore.latest_pending(
            **self.owner,
            session_id=self.session_id,
            workdir=self.workdir,
            settings=self.settings,
        )
        if record is None:
            return {
                "schema": RESEARCH_WORKSPACE_SCHEMA,
                "action": "pending_action",
                "status": "none",
                "message": "当前会话没有等待确认的研究操作。",
                "pending_action": None,
            }
        presentation = record.payload.get("presentation")
        presentation = dict(presentation) if isinstance(presentation, dict) else {}
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "pending_action",
            "status": "pending",
            "message": "当前会话有一项等待确认的研究操作。",
            "pending_action": {
                **record.to_dict(),
                "title": str(presentation.get("title") or "研究操作"),
                "message": str(presentation.get("message") or ""),
                "confirmation_hint": "请明确回复“确认”或“取消”。",
            },
        }

    def list_projects(
        self,
        *,
        include_archived: bool = False,
        query: str = "",
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        projects = self._projects(include_archived=include_archived)
        normalized = str(query or "").strip().casefold()
        if normalized:
            projects = [
                item
                for item in projects
                if normalized in item.title.casefold()
                or normalized in item.description.casefold()
            ]
        selected, pagination = _page(projects, page=page, page_size=page_size)
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "list_projects",
            "items": [self._project_summary(item) for item in selected],
            "pagination": pagination,
        }

    def list_candidates(
        self,
        *,
        include_inactive: bool = False,
        query: str = "",
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        candidates = self._candidates(include_inactive=include_inactive)
        normalized = str(query or "").strip().casefold()
        if normalized:
            candidates = [
                item
                for item in candidates
                if normalized in " ".join(
                    [item.suggested_title, item.goal_summary, *item.keywords]
                ).casefold()
            ]
        selected, pagination = _page(candidates, page=page, page_size=page_size)
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "list_candidates",
            "items": [self._candidate_summary(item) for item in selected],
            "pagination": pagination,
        }

    def candidate_detail(
        self,
        candidate_id: str,
        *,
        source_category: str = "",
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        candidate = self._candidate(candidate_id)
        events = ResearchTopicStore.list_candidate_events(
            candidate_id=candidate.candidate_id,
            **self.owner,
            limit=5000,
            workdir=self.workdir,
            settings=self.settings,
        )
        category_filter = str(source_category or "").strip().lower()
        if category_filter and category_filter not in _SOURCE_CATEGORIES:
            raise ValueError(f"unsupported research source category: {category_filter}")
        counts = {category: 0 for category in _SOURCE_CATEGORIES}
        technical_count = 0
        sources: list[dict[str, Any]] = []
        for event in reversed(events):
            if _is_technical_event(event):
                technical_count += 1
                continue
            category = _event_category(event)
            counts[category] += 1
            if category_filter and category != category_filter:
                continue
            title = _source_title(event, category)
            sources.append(
                {
                    "event_id": event.event_id,
                    "category": category,
                    "title": title,
                    "summary": _readable_payload(event) or title,
                    "occurred_at": event.occurred_at,
                    "actor": "用户" if event.actor_type == "user" else "Agent",
                }
            )
        selected, pagination = _page(sources, page=page, page_size=page_size)
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "candidate_detail",
            "candidate": self._candidate_summary(candidate),
            "candidate_digest": research_candidate_digest(candidate),
            "support_counts": counts,
            "readable_support_count": sum(counts.values()),
            "technical_record_count": technical_count,
            "sources": selected,
            "pagination": pagination,
        }

    def propose_candidate_confirmation(self, candidate_id: str) -> dict[str, Any]:
        detail = self.candidate_detail(candidate_id, page=1, page_size=5)
        candidate = dict(detail["candidate"])
        active_projects = [
            item for item in self._projects(include_archived=False) if item.status != "archived"
        ]
        if candidate["state"] == "confirmed":
            status = "completed"
            reason = "该候选已经是正式课题。"
        elif candidate["state"] not in {"emerging", "stable"}:
            status = "blocked"
            reason = "该候选已经失效，不能再确认为正式课题。"
        elif active_projects:
            status = "blocked"
            reason = "当前 Workspace 已有正式课题；一个 Workspace 只能保留一个未归档正式课题。"
        else:
            status = "ready"
            reason = "确认后，该候选将成为当前 Workspace 的正式课题。"
        action = {
            "schema": RESEARCH_ACTION_SCHEMA,
            "kind": "candidate_confirmation",
            "status": status,
            "title": candidate["title"],
            "message": reason,
            "candidate_id": candidate["candidate_id"],
            "candidate_digest": detail["candidate_digest"],
            "candidate": detail["candidate"],
            "support_counts": detail["support_counts"],
            "readable_support_count": detail["readable_support_count"],
            "technical_record_count": detail["technical_record_count"],
            "representative_sources": detail["sources"],
            "confirmed_research_id": candidate["confirmed_research_id"],
        }
        self._attach_pending_action(
            action,
            target_type="candidate",
            target_id=candidate["candidate_id"],
            target_digest=str(detail["candidate_digest"] or ""),
            operation={
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": detail["candidate_digest"],
            },
        )
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "propose_confirm_candidate",
            "status": status,
            "message": reason,
            "research_action": action,
        }

    def propose_candidate_rename(
        self,
        candidate_id: str,
        *,
        title: str,
    ) -> dict[str, Any]:
        detail = self.candidate_detail(candidate_id, page=1, page_size=5)
        candidate = dict(detail["candidate"])
        proposed_title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not proposed_title:
            raise ValueError("新的候选课题名称不能为空")
        if len(proposed_title) > 160:
            raise ValueError("新的候选课题名称不能超过 160 个字符")
        if candidate["state"] in {"merged", "split", "dismissed"}:
            status = "blocked"
            reason = "该候选已进入历史状态，不能再重命名。"
        elif proposed_title == candidate["title"]:
            status = "completed"
            reason = "新的名称与当前名称相同，本次无需修改。"
        elif candidate["state"] == "confirmed":
            status = "ready"
            reason = "确认后将同时修改候选名称和对应正式课题名称，并保留重命名记录。"
        else:
            status = "ready"
            reason = "确认后只修改候选课题名称，不改变其来源记录和归属。"
        action = {
            "schema": RESEARCH_ACTION_SCHEMA,
            "kind": "candidate_rename",
            "status": status,
            "title": f"重命名候选课题：{candidate['title']}",
            "message": reason,
            "candidate_id": candidate["candidate_id"],
            "candidate_digest": detail["candidate_digest"],
            "candidate": detail["candidate"],
            "current_title": candidate["title"],
            "proposed_title": proposed_title,
            "support_counts": detail["support_counts"],
            "readable_support_count": detail["readable_support_count"],
            "technical_record_count": detail["technical_record_count"],
        }
        self._attach_pending_action(
            action,
            target_type="candidate",
            target_id=candidate["candidate_id"],
            target_digest=str(detail["candidate_digest"] or ""),
            operation={
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": detail["candidate_digest"],
                "title": proposed_title,
            },
        )
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "propose_rename_candidate",
            "status": status,
            "message": reason,
            "research_action": action,
        }

    def propose_candidate_dismissal(self, candidate_id: str) -> dict[str, Any]:
        detail = self.candidate_detail(candidate_id, page=1, page_size=5)
        candidate = dict(detail["candidate"])
        if candidate["state"] == "confirmed":
            status = "blocked"
            reason = "该候选已成为正式课题；请先在正式课题中归档，不能作为普通候选忽略。"
        elif candidate["state"] in {"merged", "split", "dismissed"}:
            status = "blocked"
            reason = "该候选已进入历史状态，无需再次忽略。"
        else:
            status = "ready"
            reason = (
                "确认后候选会移入历史记录，关联事件退回待整理；"
                "原始 Session、文件和审计记录都不会被删除。"
            )
        action = {
            "schema": RESEARCH_ACTION_SCHEMA,
            "kind": "candidate_dismissal",
            "status": status,
            "title": f"忽略候选课题：{candidate['title']}",
            "message": reason,
            "candidate_id": candidate["candidate_id"],
            "candidate_digest": detail["candidate_digest"],
            "candidate": detail["candidate"],
            "current_title": candidate["title"],
            "support_counts": detail["support_counts"],
            "readable_support_count": detail["readable_support_count"],
            "technical_record_count": detail["technical_record_count"],
        }
        self._attach_pending_action(
            action,
            target_type="candidate",
            target_id=candidate["candidate_id"],
            target_digest=str(detail["candidate_digest"] or ""),
            operation={
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": detail["candidate_digest"],
            },
        )
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "propose_dismiss_candidate",
            "status": status,
            "message": reason,
            "research_action": action,
        }

    def experiment_runs(self, *, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        from ..workflow.manager import WorkflowRunManager
        rows = [run for run in WorkflowRunManager.list_runs(workdir=self.workdir)
                if all(str((run.metadata or {}).get(k) or "") in {"", v} for k, v in self.owner.items())]
        selected, pagination = _page(rows, page=page, page_size=page_size)
        return {"schema": RESEARCH_WORKSPACE_SCHEMA, "action": "list_experiment_runs", "pagination": pagination,
                "items": [{"run_id": r.run_id, "title": r.template_key, "status": r.status, "updated_at": r.updated_at} for r in selected],
                "message": "列出已有运行，不启动计算。请根据实际目标选择，不凭名称猜测唯一运行。"}

    def research_branches(self, *, research_id: str = "", page: int = 1, page_size: int = 10) -> dict[str, Any]:
        project = self._resolve_project(research_id)
        rows = ResearchExperimentStore.list_experiments(project.research_id, **self.owner, workdir=self.workdir)
        selected, pagination = _page(rows, page=page, page_size=page_size)
        hypotheses = ResearchExperimentStore.list_hypotheses(project.research_id, **self.owner, workdir=self.workdir)
        from .boltz_rounds import rounds_for
        rounds = rounds_for(self.workdir, [item.experiment_id for item in selected])
        return {"schema": RESEARCH_WORKSPACE_SCHEMA, "action": "list_research_branches",
                "project": self._project_summary(project), "items": [
                    {**r.to_dict(), "protein_runs": rounds[r.experiment_id]} for r in selected],
                "hypotheses": [h.to_dict() for h in hypotheses[:20]], "hypothesis_count": len(hypotheses),
                "pagination": pagination, "message": "实验记录不代表计算已启动；冻结后可创建后续路线。"}

    def research_hypotheses(self, *, research_id: str = "", page: int = 1, page_size: int = 10) -> dict[str, Any]:
        project = self._resolve_project(research_id)
        rows = ResearchExperimentStore.list_hypotheses(project.research_id, **self.owner, workdir=self.workdir)
        selected, pagination = _page(rows, page=page, page_size=page_size)
        return {"schema": RESEARCH_WORKSPACE_SCHEMA, "action": "list_research_hypotheses",
                "items": [h.to_dict() for h in selected], "pagination": pagination}

    def research_step_preview(self, operation: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Validate owner-scoped targets and snapshot exactly what the user approves."""
        project = self._resolve_project(str(operation.get("research_id") or ""))
        if project.status == "archived":
            raise ValueError("归档课题只读，请先恢复课题。")
        owner = {**self.owner, "workdir": self.workdir}
        hypotheses = ResearchExperimentStore.list_hypotheses(project.research_id, **owner)
        hypothesis = next((h for h in hypotheses if h.hypothesis_id == operation.get("hypothesis_id")), None)
        step = str(operation.get("step") or "")
        details: dict[str, Any] = {"课题": project.title, "操作": _RESEARCH_STEP_LABELS.get(step, step)}
        dependencies: dict[str, Any] = {"project": project.to_dict(), "operation": operation}
        if step == "create_hypothesis":
            if not str(operation.get("statement") or "").strip():
                raise ValueError("请说明假说内容。")
            details.update({"假说": operation["statement"], "验证计划": operation.get("test_plan", ""),
                            "证伪条件": operation.get("falsification_condition", ""), "影响": "只创建待批准假说，不批准、不运行计算。"})
        elif step == "link_hypothesis_evidence":
            from .hypothesis_map import HypothesisMap, RELATIONS
            from .knowledge_store import ResearchKnowledgeStore
            if hypothesis is None:
                raise ValueError("该假说不属于当前课题，请先确认目标。")
            kind, oid = str(operation.get("object_type") or ""), str(operation.get("object_id") or "")
            relation = str(operation.get("relation") or "")
            reason = str(operation.get("reason") or "").strip()
            if relation not in RELATIONS or not reason or len(reason) > 4000 or len(str(operation.get("scope") or "")) > 4000:
                raise ValueError("请选择关系并填写判断理由。")
            source = HypothesisMap(**owner).read_source(project.research_id, kind, oid)
            if source["status"] != "active" or not source["content"].strip():
                raise ValueError("来源缺失或没有可核对正文，请先补齐资料。")
            existing = next((l for l in ResearchKnowledgeStore.list_links(project.research_id, **owner, limit=10000)
                if l.subject_type == "hypothesis" and l.subject_id == hypothesis.hypothesis_id and l.object_type == kind and l.object_id == oid), None)
            if existing and existing.discovered_by == "user":
                raise ValueError("此关系已有用户核对记录，请在假说与证据地图中读取来源并修改。")
            dependencies.update(hypothesis=hypothesis.to_dict(), source_hash=source["content_hash"], link=existing.to_dict() if existing else None)
            details.update({"假说": hypothesis.statement, "来源": source["title"], "关系": RELATIONS[relation], "判断理由": reason,
                "适用条件": operation.get("scope", ""), "影响": "仅保存待核对关系，不确认科研结论，不启动计算。请在地图中核对原文。"})
        elif step in {"approve_hypothesis", "create_experiment"}:
            if hypothesis is None:
                raise ValueError("该假说不属于当前课题，请先列出假说并确认目标。")
            dependencies["hypothesis"] = hypothesis.to_dict()
            details.update({"假说": hypothesis.statement, "假说版本": hypothesis.revision})
            if step == "approve_hypothesis":
                if hypothesis.status != "proposed":
                    raise ValueError("仅待批准假说可以批准。")
                details["影响"] = "批准该假说供实验引用；不会创建实验或启动计算。"
            else:
                if str(operation.get("workflow_run_id") or "").strip():
                    raise ValueError("创建记录与关联运行是独立操作；请先创建记录，再使用 attach_workflow 预览已有运行关联。")
                if hypothesis.status not in {"approved", "testing"}:
                    raise ValueError("请先批准假说，再创建实验记录。")
                if not str(operation.get("title") or "").strip():
                    raise ValueError("请提供实验名称。")
                mode = operation.get("execution_mode", "shared_workspace")
                if mode not in {"shared_workspace", "isolated_directory", "git_worktree"}:
                    raise ValueError("无效执行位置。")
                parent_id = operation.get("parent_experiment_id")
                if parent_id:
                    parent = ResearchExperimentStore.get_experiment(parent_id, **owner)
                    if parent.research_id != project.research_id or parent.hypothesis_id != hypothesis.hypothesis_id or not parent.frozen_at:
                        raise ValueError("后续路线必须引用同一课题、同一假说的已冻结实验。")
                    dependencies["parent"] = parent.to_dict()
                    details.update({"基于实验": parent.title, "沿用输入": parent.output_artifacts})
                details.update({"新路线": operation["title"], "本次改变与目的": operation.get("reason", ""),
                                "执行位置": {"shared_workspace": "共享工作区", "isolated_directory": "独立目录", "git_worktree": "Git 隔离工作区"}[mode],
                                "影响": "仅创建实验记录并准备执行位置，不提交计算、不自动运行工具。计算需要另行确认；输入引用不等于文件或环境已复制。"})
        elif step in {"complete_experiment", "attach_workflow"}:
            experiment = ResearchExperimentStore.get_experiment(str(operation.get("experiment_id") or ""), **owner)
            if experiment.research_id != project.research_id or experiment.frozen_at:
                raise ValueError("该实验不属于当前课题或已经冻结；请创建后续路线。")
            dependencies["experiment"] = experiment.to_dict()
            details["实验"] = experiment.title
            if step == "complete_experiment":
                if operation.get("decision") not in {"recommended", "retained", "rejected", "merged", "inconclusive"}:
                    raise ValueError("请明确实验判断。")
                if not str(operation.get("result_summary") or "").strip():
                    raise ValueError("完成实验前请说明结果与判断原因。")
                details.update({"结果与理由": operation["result_summary"], "判断": {"recommended":"推荐继续", "retained":"保留备选", "rejected":"不再推进", "merged":"成果纳入研究（不是 Git 合并）", "inconclusive":"尚无定论"}[operation["decision"]],
                                "影响": "标记完成并冻结当前记录；Git 隔离模式可能提交工作区变更。后续需新建路线。合并仅记录研究判断，不执行 Git 合并。"})
            else:
                from ..workflow.manager import WorkflowRunManager
                run = WorkflowRunManager.get(str(operation.get("workflow_run_id") or ""), workdir=self.workdir)
                if run is None or any(str((run.metadata or {}).get(k) or "") not in {"", v} for k, v in self.owner.items()):
                    raise ValueError("计算运行不存在或不属于当前工作区。")
                dependencies["run"] = run.to_dict()
                details.update({"计算运行": run.run_id, "影响": "仅关联已有计算运行，不启动、重启或修改其执行状态。"})
        else:
            raise ValueError("不支持该研究操作；启动计算必须使用独立的计算提交流程。")
        return details, sha256_text(canonical_json(dependencies))

    def propose_research_step(self, operation: dict[str, Any]) -> dict[str, Any]:
        details, digest = self.research_step_preview(operation)
        action = {"schema": RESEARCH_ACTION_SCHEMA, "kind": "research_step", "status": "ready",
                  "title": _RESEARCH_STEP_LABELS[operation["step"]], "message": details["影响"], "details": details,
                  "research_id": operation.get("research_id", "")}
        self._attach_pending_action(action, target_type="research_step", target_id=str(operation.get("experiment_id") or operation.get("hypothesis_id") or operation.get("research_id") or "current"), target_digest=digest, operation=operation)
        return {"schema": RESEARCH_WORKSPACE_SCHEMA, "action": "propose_research_step", "status": "ready", "research_action": action}

    def compare_branches(self, left_id: str, right_id: str) -> dict[str, Any]:
        owner = {**self.owner, "workdir": self.workdir}
        left = ResearchExperimentStore.get_experiment(left_id, **owner)
        right = ResearchExperimentStore.get_experiment(right_id, **owner)
        if left.research_id != right.research_id or left_id == right_id:
            raise ValueError("请选择同一课题下两个不同实验。")
        fields = ("hypothesis_revision", "reason", "result_summary", "decision", "metrics", "input_artifacts", "output_artifacts", "run_snapshot")
        a, b = left.to_dict(), right.to_dict()
        return {"schema": RESEARCH_WORKSPACE_SCHEMA, "action": "compare_research_branches", "left": a, "right": b,
                "differences": {f: {"left": a.get(f), "right": b.get(f)} for f in fields if a.get(f) != b.get(f)},
                "message": "这是记录与运行快照的对比，不自动证明科学优劣或可复现性。"}

    def list_packs(
        self,
        *,
        research_id: str = "",
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        if research_id:
            self._project(research_id)
        exports = ResearchPackStore.list_exports(
            research_id=str(research_id or "").strip(),
            **self.owner,
            limit=200,
            workdir=self.workdir,
            settings=self.settings,
        )
        selected, pagination = _page(exports, page=page, page_size=page_size)
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "list_packs",
            "items": [item.to_dict() for item in selected],
            "pagination": pagination,
        }

    def _pack_preview_action(
        self,
        project: ResearchProject,
        *,
        privacy_profile: str,
        persist_action: bool = True,
    ) -> dict[str, Any]:
        if not self.settings.research_pack_active:
            raise ValueError("ResearchPack export is not active")
        preview = ResearchPackV2Builder(
            **self.owner,
            workdir=self.workdir,
            settings=self.settings,
        ).preview_scope(
            ResearchPackV2Scope(mode="project", research_ids=[project.research_id]),
            privacy_profile=privacy_profile,
            attribution_mode="anonymous",
        )
        preview_value = preview.to_dict()
        action = {
            "schema": RESEARCH_ACTION_SCHEMA,
            "kind": "pack_build",
            "status": "ready" if preview.export_ready else "blocked",
            "title": project.title,
            "message": (
                "研究包已经通过预览，可以确认生成。"
                if preview.export_ready
                else "研究包预览存在阻断项，请先处理后再生成。"
            ),
            "research_id": project.research_id,
            "privacy_profile": privacy_profile,
            "preview": {
                "source_digest": preview.source_digest,
                "export_ready": preview.export_ready,
                "estimated_bytes": preview.estimated_bytes,
                "counts": preview.counts,
                "exclusions": preview.exclusions,
                "privacy_report": preview.privacy_report,
                "issues": preview.issues[:20],
            },
        }
        if persist_action:
            self._attach_pending_action(
                action,
                target_type="research",
                target_id=project.research_id,
                target_digest=preview.source_digest,
                operation={
                    "research_id": project.research_id,
                    "privacy_profile": privacy_profile,
                    "source_digest": preview.source_digest,
                },
            )
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "preview_pack",
            "status": action["status"],
            "project": self._project_summary(project),
            "preview": preview_value,
            "research_action": action,
        }

    def preview_pack(
        self,
        *,
        research_id: str = "",
        query: str = "",
        privacy_profile: str = "strict",
    ) -> dict[str, Any]:
        project = self._resolve_project(research_id, query)
        return self._pack_preview_action(project, privacy_profile=privacy_profile)

    def prepare_download(
        self,
        *,
        research_id: str = "",
        query: str = "",
        pack_id: str = "",
        privacy_profile: str = "strict",
    ) -> dict[str, Any]:
        normalized_pack_id = str(pack_id or "").strip()
        if normalized_pack_id:
            export = ResearchPackStore.get_export(
                normalized_pack_id,
                **self.owner,
                workdir=self.workdir,
                settings=self.settings,
            )
            if export is None:
                raise ValueError("ResearchPack was not found in the current workspace")
            if ResearchPackStore.archive_file(
                export,
                workdir=self.workdir,
                settings=self.settings,
            ) is None:
                raise ValueError("ResearchPack archive is not ready")
            return self._download_action(export)

        project = self._resolve_project(research_id, query)
        preview_result = self._pack_preview_action(
            project,
            privacy_profile=privacy_profile,
            persist_action=False,
        )
        preview = dict(preview_result["preview"])
        exports = ResearchPackStore.list_exports(
            project.research_id,
            **self.owner,
            limit=50,
            workdir=self.workdir,
            settings=self.settings,
        )
        current = next(
            (
                item
                for item in exports
                if item.status == "completed"
                and item.source_digest == str(preview.get("source_digest") or "")
                and item.privacy_profile == privacy_profile
                and ResearchPackStore.archive_file(
                    item,
                    workdir=self.workdir,
                    settings=self.settings,
                )
                is not None
            ),
            None,
        )
        if current is not None:
            return self._download_action(current, project=project)
        preview_result["action"] = "prepare_download"
        preview_result["message"] = (
            "当前没有与最新研究状态一致的研究包；请确认生成后下载。"
            if preview_result["status"] == "ready"
            else "最新研究状态尚不能打包，请先处理阻断项。"
        )
        preview_result["research_action"]["message"] = preview_result["message"]
        self._attach_pending_action(
            preview_result["research_action"],
            target_type="research",
            target_id=project.research_id,
            target_digest=str(preview.get("source_digest") or ""),
            operation={
                "research_id": project.research_id,
                "privacy_profile": privacy_profile,
                "source_digest": str(preview.get("source_digest") or ""),
            },
        )
        return preview_result

    def _download_action(
        self,
        export,
        *,
        project: ResearchProject | None = None,
    ) -> dict[str, Any]:
        if project is None and str(export.research_id or "").strip():
            project = self._project(export.research_id)
        title = (
            project.title
            if project is not None
            else str(dict(export.manifest or {}).get("title") or "Workspace ResearchPack")
        )
        export_value = export.to_dict()
        action = {
            "schema": RESEARCH_ACTION_SCHEMA,
            "kind": "pack_download",
            "status": "ready",
            "title": title,
            "message": "与当前研究状态一致的研究包已经可以下载。",
            "research_id": project.research_id if project is not None else export.research_id,
            "pack_id": export.pack_id,
            "archive_name": export.archive_name,
            "archive_size": export.archive_size,
            "export": export_value,
        }
        return {
            "schema": RESEARCH_WORKSPACE_SCHEMA,
            "action": "prepare_download",
            "status": "ready",
            "project": self._project_summary(project) if project is not None else None,
            "export": export_value,
            "research_action": action,
        }


__all__ = [
    "RESEARCH_ACTION_SCHEMA",
    "RESEARCH_WORKSPACE_SCHEMA",
    "ResearchWorkspaceService",
]
