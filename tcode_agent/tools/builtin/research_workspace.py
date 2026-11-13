"""Natural-language control plane for the current research workspace."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from pydantic import Field

from ...research.workspace_service import ResearchWorkspaceService
from ...types import ToolResult
from ..base import ParameterSchema, Tool, ToolContext


INTELLIGENT_UI_ACTIONS = {"show_hypothesis_map", "compare_research_snapshots"}


ResearchWorkspaceAction = Literal[
    "show_hypothesis_map",
    "list_hypothesis_sources",
    "overview",
    "pending_action",
    "list_projects",
    "list_candidates",
    "candidate_detail",
    "propose_confirm_candidate",
    "propose_rename_candidate",
    "propose_dismiss_candidate",
    "preview_pack",
    "list_packs",
    "prepare_download",
    "list_research_branches",
    "list_research_hypotheses",
    "list_experiment_runs",
    "compare_research_branches",
    "propose_research_step",
    "save_research_snapshot",
    "list_research_snapshots",
    "compare_research_snapshots",
]


class ResearchWorkspaceParameters(ParameterSchema):
    action: ResearchWorkspaceAction = "overview"
    candidate_id: str = Field(default="", max_length=160)
    title: str = Field(default="", max_length=160)
    research_id: str = Field(default="", max_length=160)
    pack_id: str = Field(default="", max_length=180)
    query: str = Field(default="", max_length=500)
    source_category: str = Field(default="", max_length=40)
    include_archived: bool = False
    include_inactive: bool = False
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=10, ge=1, le=20)
    privacy_profile: str = Field(default="strict", max_length=40)
    step: Literal["create_hypothesis", "approve_hypothesis", "create_experiment", "complete_experiment", "attach_workflow", "link_hypothesis_evidence"] = "create_experiment"
    hypothesis_id: str = Field(default="", max_length=160)
    object_type: Literal["evidence", "paper", "passage", "wiki_page", "wiki_claim"] = "evidence"
    object_id: str = Field(default="", max_length=240)
    relation: Literal["supports", "contradicts", "context_only", "references"] = "references"
    scope: str = Field(default="", max_length=4000)
    experiment_id: str = Field(default="", max_length=160)
    parent_experiment_id: str = Field(default="", max_length=160)
    other_experiment_id: str = Field(default="", max_length=160)
    workflow_run_id: str = Field(default="", max_length=240)
    statement: str = Field(default="", max_length=4000)
    rationale: str = Field(default="", max_length=4000)
    test_plan: str = Field(default="", max_length=4000)
    falsification_condition: str = Field(default="", max_length=4000)
    reason: str = Field(default="", max_length=4000)
    result_summary: str = Field(default="", max_length=4000)
    decision: Literal["recommended", "retained", "rejected", "merged", "inconclusive"] = Field(
        default="inconclusive",
        description="完成实验时的判断：recommended=推荐继续；retained=保留备选；rejected=不再推进；merged=成果纳入研究（不是 Git 合并）；inconclusive=尚无定论。",
    )
    execution_mode: Literal["shared_workspace", "isolated_directory", "git_worktree"] = "shared_workspace"
    base_snapshot_id: str = Field(default="", max_length=160)
    head_snapshot_id: str = Field(default="", max_length=160)
    request_id: str = Field(default="", max_length=180)


class ResearchWorkspaceTool(Tool):
    @property
    def id(self) -> str:
        return "research_workspace"

    @property
    def description(self) -> str:
        return """Inspect and safely operate the current user's Research Workspace.

Use overview/list_projects for questions such as how many formal research topics
exist. Use list_candidates and candidate_detail to explain candidate topics and
their human-readable support from messages, files, tools, MCP, agents, workflows,
and artifacts. Use propose_confirm_candidate when the user asks to make a
specific candidate the formal topic. Use propose_rename_candidate when the user
asks to rename a candidate, and propose_dismiss_candidate when the user asks to
ignore/remove a candidate from the active list. These actions only return a user
confirmation card and never mutate data by themselves. Candidate dismissal is
auditable: it preserves the original sessions/files and returns associated events
to pending review; there is no hard-delete candidate action. Use prepare_download when the user asks to
download the latest ResearchPack: it returns either a current download card, a
safe build-confirmation card, or concrete blockers. Use preview_pack to check
whether a pack can be built and list_packs to inspect prior exports.
Use pending_action when the user asks what is currently waiting for confirmation.
After a proposal, tell the user they may reply “确认” or “取消”; on that next turn
use research_action_commit, not this tool, to submit the decision.

Never invent candidate_id, research_id, or pack_id. Query/list first when the
target is ambiguous. Scope always comes from the authenticated session, not from
tool arguments. For deep reading of an existing project's findings, episodes,
evidence, or handoff, use research_read instead.

For research routes: list_research_branches lists experiments and hypotheses;
list_research_hypotheses paginates the full hypothesis list beyond its summary;
list_experiment_runs lists existing owner-scoped calculation runs for association;
compare_research_branches reads two experiment snapshots. Use propose_research_step
to propose create_hypothesis, approve_hypothesis, create_experiment (optionally
parent_experiment_id for another route), complete_experiment, or attach_workflow.
Explain the hypothesis, parent route, concrete change/reason, inherited inputs,
and side effects BEFORE confirmation. Do not invent hypothesis/experiment/run IDs.
If a reference such as 'the last experiment' is ambiguous, ask the user to choose.
Creation only makes a planned record and prepares an execution location. It does
NOT submit computation. To actually run, use the separate existing workflow/job
submission flow, confirm inputs/resources with the user, then associate its run
via attach_workflow. Never interpret approval of creation as approval to run.
Completion freezes the record; merged means a research decision, NOT Git merge.
Hypothesis creation never implies approval. All proposed writes require the next
explicit user decision through research_action_commit.

Use list_hypothesis_sources to discover project-owned source IDs and titles. Use propose_research_step with step=link_hypothesis_evidence, hypothesis_id, object_type/object_id, relation, reason and scope to propose saving an evidence relationship. Confirmation saves a pending judgement; it does not confirm the original or prove the hypothesis. Never replace a user-reviewed relationship automatically.

Use show_hypothesis_map when the user asks why a hypothesis is believed, wants a clickable evidence map, counterevidence, or competing explanations. It reads saved references and attaches a clickable summary; it never confirms evidence or writes relationships. Missing sources and pending review are not verified facts. Never output opaque IDs or English enum codes in the answer.

For changes over time, use list_research_snapshots and compare_research_snapshots.
The latter produces an interactive summary with links into the Research Center.
User-facing answers must use saved object titles, readable status descriptions and
version names, not internal IDs, raw field names or serialized JSON. Use the
before_names/after_names for referenced objects; never invent a missing name.
Never compare current prose summaries as historical facts or invent snapshot IDs.
Use save_research_snapshot only when the user asks to save a research version;
it records current state without changing findings or launching computation.
Snapshots start when saved; earlier missing history cannot be reconstructed.
Research snapshot saving is separate from proposed research fact mutations."""

    @property
    def parameters(self) -> type:
        return ResearchWorkspaceParameters

    @staticmethod
    def _service(context: ToolContext) -> ResearchWorkspaceService:
        extra = dict(context.extra or {})
        return ResearchWorkspaceService(
            tenant_id=str(extra.get("tenant_id") or ""),
            user_id=str(extra.get("user_id") or ""),
            workspace_id=str(extra.get("workspace_id") or ""),
            session_id=str(context.session_id or ""),
            proposal_message_id=str(context.message_id or ""),
            workdir=str(extra.get("workdir") or ""),
        )

    @staticmethod
    def _model_result(result: dict[str, Any]) -> dict[str, Any]:
        if result.get("schema") == "tcode.research-changes.v1":
            from ...research.snapshots import comparison_summary
            return comparison_summary(result)
        bounded = dict(result)
        if "假说" in bounded:
            bounded.pop("research_id", None)
        action = result.get("research_action")
        if isinstance(action, dict) and "preview" in bounded:
            bounded["preview"] = dict(action.get("preview") or {})
        return bounded

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            parsed = ResearchWorkspaceParameters(**args)
            action = parsed.action
            if action in {
                "propose_confirm_candidate",
                "propose_rename_candidate",
                "propose_dismiss_candidate",
                "preview_pack",
                "prepare_download",
                "propose_research_step",
                "save_research_snapshot",
            } and str(dict(context.extra or {}).get("parent_session_id") or "").strip():
                raise ValueError(
                    "subagents may inspect research state but cannot propose user-facing research actions"
                )
            service = self._service(context)
            if action in INTELLIGENT_UI_ACTIONS:
                from ...server.auth import intelligent_ui_preference
                preference = await asyncio.to_thread(
                    intelligent_ui_preference, service.owner["tenant_id"], service.owner["user_id"],
                )
                if not preference["effective_enabled"]:
                    return ToolResult(title="智能 UI 已关闭", output="", error="intelligent_ui_generation_disabled")
            if action in {"save_research_snapshot", "list_research_snapshots", "compare_research_snapshots"}:
                from ...research.snapshots import ResearchSnapshotStore
                store = ResearchSnapshotStore(**service.owner, workdir=service.workdir)
                if action == "save_research_snapshot":
                    from ...research.models import sha256_text
                    result = store.capture(parsed.research_id, parsed.title, parsed.request_id or sha256_text(f"{context.session_id}:{context.message_id}:{parsed.title}"))
                elif action == "list_research_snapshots":
                    result = store.list(parsed.research_id, offset=(parsed.page - 1) * 50)
                else:
                    result = store.compare(parsed.base_snapshot_id, parsed.head_snapshot_id, offset=(parsed.page - 1) * parsed.page_size, limit=parsed.page_size)
            elif action == "overview":
                result = service.overview()
            elif action == "list_hypothesis_sources":
                from ...research.hypothesis_map import HypothesisMap
                project = service._resolve_project(parsed.research_id)
                result = HypothesisMap(**service.owner, workdir=service.workdir).source_choices(project.research_id, offset=(parsed.page-1)*parsed.page_size, limit=parsed.page_size)
            elif action == "show_hypothesis_map":
                from ...research.hypothesis_map import HypothesisMap, hypothesis_map_summary
                project = service._resolve_project(parsed.research_id)
                graph = HypothesisMap(**service.owner, workdir=service.workdir).read(project.research_id, offset=(parsed.page-1)*10, hypothesis_id=parsed.hypothesis_id)
                result = hypothesis_map_summary(graph)
                result["research_id"] = project.research_id
            elif action == "list_research_branches":
                result = service.research_branches(research_id=parsed.research_id, page=parsed.page, page_size=parsed.page_size)
            elif action == "list_experiment_runs":
                result = service.experiment_runs(page=parsed.page, page_size=parsed.page_size)
            elif action == "list_research_hypotheses":
                result = service.research_hypotheses(research_id=parsed.research_id, page=parsed.page, page_size=parsed.page_size)
            elif action == "compare_research_branches":
                result = service.compare_branches(parsed.experiment_id, parsed.other_experiment_id)
            elif action == "propose_research_step":
                fields = ("step", "research_id", "hypothesis_id", "experiment_id", "parent_experiment_id", "title", "statement", "rationale", "test_plan", "falsification_condition", "reason", "result_summary", "decision", "execution_mode", "workflow_run_id", "object_type", "object_id", "relation", "scope")
                operation = {key: getattr(parsed, key) for key in fields}
                result = service.propose_research_step(operation)
            elif action == "pending_action":
                result = service.pending_action()
            elif action == "list_projects":
                result = service.list_projects(
                    include_archived=parsed.include_archived,
                    query=parsed.query,
                    page=parsed.page,
                    page_size=parsed.page_size,
                )
            elif action == "list_candidates":
                result = service.list_candidates(
                    include_inactive=parsed.include_inactive,
                    query=parsed.query,
                    page=parsed.page,
                    page_size=parsed.page_size,
                )
            elif action == "candidate_detail":
                if not parsed.candidate_id.strip():
                    raise ValueError("candidate_id is required for candidate_detail")
                result = service.candidate_detail(
                    parsed.candidate_id,
                    source_category=parsed.source_category,
                    page=parsed.page,
                    page_size=parsed.page_size,
                )
            elif action == "propose_confirm_candidate":
                if not parsed.candidate_id.strip():
                    raise ValueError("candidate_id is required for propose_confirm_candidate")
                result = service.propose_candidate_confirmation(parsed.candidate_id)
            elif action == "propose_rename_candidate":
                if not parsed.candidate_id.strip():
                    raise ValueError("candidate_id is required for propose_rename_candidate")
                if not parsed.title.strip():
                    raise ValueError("title is required for propose_rename_candidate")
                result = service.propose_candidate_rename(
                    parsed.candidate_id,
                    title=parsed.title,
                )
            elif action == "propose_dismiss_candidate":
                if not parsed.candidate_id.strip():
                    raise ValueError("candidate_id is required for propose_dismiss_candidate")
                result = service.propose_candidate_dismissal(parsed.candidate_id)
            elif action == "preview_pack":
                result = service.preview_pack(
                    research_id=parsed.research_id,
                    query=parsed.query,
                    privacy_profile=parsed.privacy_profile,
                )
            elif action == "list_packs":
                result = service.list_packs(
                    research_id=parsed.research_id,
                    page=parsed.page,
                    page_size=parsed.page_size,
                )
            elif action == "prepare_download":
                result = service.prepare_download(
                    research_id=parsed.research_id,
                    query=parsed.query,
                    pack_id=parsed.pack_id,
                    privacy_profile=parsed.privacy_profile,
                )
            else:  # pragma: no cover - guarded by Pydantic Literal
                raise ValueError(f"unsupported research workspace action: {action}")

            output = json.dumps(
                self._model_result(result),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            truncated = len(output) > 32_000
            if truncated:
                output = json.dumps(
                    {
                        "schema": result.get("schema"),
                        "action": action,
                        "status": "blocked",
                        "message": "结果超过工具预算，请使用分页或更精确的查询。",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            metadata: dict[str, Any] = {
                "action": action,
                "truncated": truncated,
            }
            research_action = result.get("research_action")
            if isinstance(research_action, dict):
                metadata["research_action"] = research_action
            if action == "show_hypothesis_map" and not truncated:
                metadata["hypothesis_map"] = {"schema": "tcode.hypothesis-map-link.v1", "research_id": result["research_id"], "workspace_id": service.owner["workspace_id"], "hypothesis_id": parsed.hypothesis_id}
            if action == "compare_research_snapshots" and not truncated:
                metadata["research_changes"] = {"schema": "tcode.research-changes-link.v1", "research_id": result["research_id"], "workspace_id": service.owner["workspace_id"], "base_snapshot_id": parsed.base_snapshot_id, "head_snapshot_id": parsed.head_snapshot_id}
            return ToolResult(
                title=f"Research workspace: {action}",
                output=output,
                metadata=metadata,
                truncated=truncated,
            )
        except (RuntimeError, ValueError, LookupError) as exc:
            return ToolResult(
                title="Research workspace",
                output="",
                error=str(exc),
                metadata={"count": 0},
            )

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())


__all__ = ["ResearchWorkspaceTool"]
