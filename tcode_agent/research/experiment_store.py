"""Minimal Hypothesis and Experiment relationship store."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import ResearchSettings, load_research_settings
from .models import canonical_json, utc_now_iso
from .store import ResearchEventStore

HYPOTHESIS_STATUSES = {"proposed", "approved", "testing", "supported", "refuted", "inconclusive", "withdrawn"}
EXPERIMENT_STATUSES = {"planned", "running", "completed", "failed", "cancelled"}
EXPERIMENT_DECISIONS = {"", "recommended", "retained", "rejected", "merged", "inconclusive"}


def _list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


@dataclass(frozen=True)
class ResearchHypothesis:
    hypothesis_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    statement: str
    rationale: str
    evidence_refs: list[str]
    counter_evidence_refs: list[str]
    test_plan: str
    falsification_condition: str
    status: str
    revision: int
    created_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "ResearchHypothesis":
        values = dict(row)
        values["evidence_refs"] = _list(values.pop("evidence_refs_json", "[]"))
        values["counter_evidence_refs"] = _list(values.pop("counter_evidence_refs_json", "[]"))
        values["revision"] = int(values["revision"])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchExperiment:
    experiment_id: str
    tenant_id: str
    user_id: str
    workspace_id: str
    research_id: str
    hypothesis_id: str
    hypothesis_revision: int
    workflow_run_id: str
    parent_experiment_id: str | None
    source_asset_id: str
    source_experiment_id: str
    title: str
    reason: str
    status: str
    decision: str
    result_summary: str
    run_snapshot: dict[str, Any]
    execution_mode: str
    execution_workdir: str
    execution_manifest: dict[str, Any]
    input_artifacts: list[dict[str, Any]]
    output_artifacts: list[dict[str, Any]]
    metrics: dict[str, Any]
    base_commit: str
    result_commit: str
    created_by: str
    created_at: str
    updated_at: str
    frozen_at: str

    @classmethod
    def from_row(cls, row: Any) -> "ResearchExperiment":
        values = dict(row)
        try:
            values["run_snapshot"] = json.loads(values.pop("run_snapshot_json", "{}") or "{}")
        except (TypeError, ValueError):
            values["run_snapshot"] = {}
        for field, default in (("execution_manifest", {}), ("input_artifacts", []), ("output_artifacts", []), ("metrics", {})):
            try:
                values[field] = json.loads(values.pop(f"{field}_json", "") or canonical_json(default))
            except (TypeError, ValueError):
                values[field] = default
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResearchExperimentStore:
    @staticmethod
    def _artifact_record(name: str, value: Any, *, stage_id: str, execution_workdir: str) -> dict[str, Any]:
        record = {"name": name, "producer_stage_id": stage_id}
        if isinstance(value, dict):
            record.update(value)
            return record
        text = str(value or "").strip()
        candidate = Path(execution_workdir, text).resolve() if execution_workdir and text else None
        if candidate and candidate.is_file():
            try:
                candidate.relative_to(Path(execution_workdir).resolve())
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                return {**record, "relative_path": candidate.relative_to(Path(execution_workdir).resolve()).as_posix(), "sha256": digest, "size": candidate.stat().st_size, "media_type": mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"}
            except ValueError:
                pass
        return {**record, "reference": value}

    @staticmethod
    def workspace_git_state(workdir: str | None) -> dict[str, Any]:
        commit = ""
        dirty: bool | None = None
        workspace_digest = ""
        if workdir:
            try:
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=workdir, check=True,
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip()
                status = subprocess.run(
                    ["git", "status", "--porcelain"], cwd=workdir, check=True,
                    capture_output=True, text=True, timeout=3,
                ).stdout
                dirty = bool(status.strip())
                diff = subprocess.run(
                    ["git", "diff", "--binary", "HEAD"], cwd=workdir, check=True,
                    capture_output=True, timeout=5,
                ).stdout
                workspace_digest = hashlib.sha256(status.encode("utf-8") + diff).hexdigest()
            except (OSError, subprocess.SubprocessError):
                pass
        return {"git_commit": commit, "git_dirty": dirty, "workspace_digest": workspace_digest}

    @staticmethod
    def workflow_run_snapshot(workflow_run_id: str, workdir: str | None = None) -> dict[str, Any]:
        """Freeze the execution facts needed for comparison and reproduction."""
        if not workflow_run_id.strip():
            return {}
        from ..workflow.manager import WorkflowRunManager
        from ..workflow.registry import WorkflowTemplateRegistry

        run = WorkflowRunManager.get(workflow_run_id.strip(), workdir=workdir)
        if run is None:
            return {}
        metadata = dict(run.metadata or {})
        execution_workdir = str(metadata.get("execution_workdir") or workdir or "")
        git_state = ResearchExperimentStore.workspace_git_state(execution_workdir)
        template = WorkflowTemplateRegistry.get(run.template_key, include_disabled=True)
        template_digest = hashlib.sha256(
            canonical_json(template.to_dict() if template else {}).encode("utf-8")
        ).hexdigest() if template else ""
        return {
            "workflow_run_id": run.run_id,
            "template_key": run.template_key,
            "template_digest": template_digest,
            "session_id": run.session_id,
            "execution_workdir": execution_workdir,
            "status": run.status,
            "input": dict(run.input_payload or {}),
            "environment": {
                "model": str(metadata.get("model") or ""),
                "backend": str(metadata.get("backend") or metadata.get("execution_backend") or ""),
                **git_state,
            },
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "status": stage.status,
                    "result_summary": stage.result_summary,
                    "artifacts": dict(stage.artifact_refs or {}),
                    "validation": dict(stage.validation_report or {}),
                }
                for stage in list(run.stage_states or [])
            ],
        }

    @staticmethod
    def _connect(workdir: str | None, settings: ResearchSettings | None = None) -> sqlite3.Connection:
        resolved = settings or load_research_settings()
        ResearchEventStore.ensure_initialized(workdir, settings=resolved)
        return ResearchEventStore._connect(workdir, settings=resolved)

    @staticmethod
    def _ensure_active_project(conn: sqlite3.Connection, research_id: str, owner: tuple[str, str, str]) -> None:
        row = conn.execute(
            "SELECT status FROM research_projects WHERE research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?",
            (research_id, *owner),
        ).fetchone()
        if row is None:
            raise LookupError("research project not found")
        if row["status"] == "archived":
            raise ValueError("archived research project is read-only")

    @classmethod
    def list_hypotheses(cls, research_id: str, *, tenant_id: str, user_id: str, workspace_id: str, workdir: str | None = None) -> list[ResearchHypothesis]:
        conn = cls._connect(workdir)
        try:
            rows = conn.execute("SELECT * FROM research_hypotheses WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? ORDER BY updated_at DESC", (tenant_id, user_id, workspace_id, research_id)).fetchall()
        finally:
            conn.close()
        return [ResearchHypothesis.from_row(row) for row in rows]

    @classmethod
    def create_hypothesis(cls, *, tenant_id: str, user_id: str, workspace_id: str, research_id: str, statement: str, rationale: str = "", test_plan: str = "", falsification_condition: str = "", evidence_refs: list[str] | None = None, counter_evidence_refs: list[str] | None = None, created_by: str = "", workdir: str | None = None) -> ResearchHypothesis:
        now = utc_now_iso()
        hypothesis_id = f"hyp-{uuid.uuid4().hex}"
        values = (hypothesis_id, tenant_id, user_id, workspace_id, research_id, statement.strip(), rationale.strip(), canonical_json(evidence_refs or []), canonical_json(counter_evidence_refs or []), test_plan.strip(), falsification_condition.strip(), "proposed", 1, created_by, now, now)
        conn = cls._connect(workdir)
        try:
            cls._ensure_active_project(conn, research_id, (tenant_id, user_id, workspace_id))
            conn.execute("INSERT INTO research_hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            conn.execute("INSERT INTO research_hypothesis_versions VALUES(?,?,?,?,?,?,?)", (hypothesis_id, 1, statement.strip(), rationale.strip(), test_plan.strip(), falsification_condition.strip(), now))
            conn.commit()
            row = conn.execute("SELECT * FROM research_hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        finally:
            conn.close()
        return ResearchHypothesis.from_row(row)

    @classmethod
    def revise_hypothesis(cls, hypothesis_id: str, *, tenant_id: str, user_id: str, workspace_id: str, statement: str, rationale: str, test_plan: str, falsification_condition: str, workdir: str | None = None) -> ResearchHypothesis:
        now = utc_now_iso()
        conn = cls._connect(workdir)
        try:
            row = conn.execute("SELECT * FROM research_hypotheses WHERE hypothesis_id=? AND tenant_id=? AND user_id=? AND workspace_id=?", (hypothesis_id, tenant_id, user_id, workspace_id)).fetchone()
            if row is None:
                raise LookupError("hypothesis not found")
            cls._ensure_active_project(conn, row["research_id"], (tenant_id, user_id, workspace_id))
            revision = int(row["revision"]) + 1
            conn.execute("UPDATE research_hypotheses SET statement=?,rationale=?,test_plan=?,falsification_condition=?,revision=?,updated_at=? WHERE hypothesis_id=?", (statement.strip(), rationale.strip(), test_plan.strip(), falsification_condition.strip(), revision, now, hypothesis_id))
            conn.execute("INSERT INTO research_hypothesis_versions VALUES(?,?,?,?,?,?,?)", (hypothesis_id, revision, statement.strip(), rationale.strip(), test_plan.strip(), falsification_condition.strip(), now))
            conn.commit()
            updated = conn.execute("SELECT * FROM research_hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        finally:
            conn.close()
        return ResearchHypothesis.from_row(updated)

    @classmethod
    def set_hypothesis_status(cls, hypothesis_id: str, status: str, *, tenant_id: str, user_id: str, workspace_id: str, workdir: str | None = None) -> ResearchHypothesis:
        if status not in HYPOTHESIS_STATUSES:
            raise ValueError("invalid hypothesis status")
        conn = cls._connect(workdir)
        try:
            current = conn.execute("SELECT research_id FROM research_hypotheses WHERE hypothesis_id=? AND tenant_id=? AND user_id=? AND workspace_id=?", (hypothesis_id, tenant_id, user_id, workspace_id)).fetchone()
            if current is None:
                raise LookupError("hypothesis not found")
            cls._ensure_active_project(conn, current["research_id"], (tenant_id, user_id, workspace_id))
            conn.execute("UPDATE research_hypotheses SET status=?,updated_at=? WHERE hypothesis_id=?", (status, utc_now_iso(), hypothesis_id))
            conn.commit()
            row = conn.execute("SELECT * FROM research_hypotheses WHERE hypothesis_id=?", (hypothesis_id,)).fetchone()
        finally:
            conn.close()
        return ResearchHypothesis.from_row(row)

    @classmethod
    def list_experiments(cls, research_id: str, *, tenant_id: str, user_id: str, workspace_id: str, workdir: str | None = None) -> list[ResearchExperiment]:
        conn = cls._connect(workdir)
        try:
            rows = conn.execute("SELECT * FROM research_experiments WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? ORDER BY created_at ASC", (tenant_id, user_id, workspace_id, research_id)).fetchall()
        finally:
            conn.close()
        return [ResearchExperiment.from_row(row) for row in rows]

    @classmethod
    def get_experiment(cls, experiment_id: str, *, tenant_id: str, user_id: str, workspace_id: str, workdir: str | None = None) -> ResearchExperiment:
        conn = cls._connect(workdir)
        try:
            row = conn.execute("SELECT * FROM research_experiments WHERE experiment_id=? AND tenant_id=? AND user_id=? AND workspace_id=?", (experiment_id, tenant_id, user_id, workspace_id)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise LookupError("experiment not found")
        return ResearchExperiment.from_row(row)

    @classmethod
    def create_experiment(cls, *, tenant_id: str, user_id: str, workspace_id: str, research_id: str, hypothesis_id: str, title: str, reason: str = "", workflow_run_id: str = "", parent_experiment_id: str | None = None, source_asset_id: str = "", source_experiment_id: str = "", created_by: str = "", execution_mode: str = "shared_workspace", input_artifacts: list[dict[str, Any]] | None = None, workdir: str | None = None) -> ResearchExperiment:
        now = utc_now_iso()
        experiment_id = f"exp-{uuid.uuid4().hex}"
        conn = cls._connect(workdir)
        try:
            cls._ensure_active_project(conn, research_id, (tenant_id, user_id, workspace_id))
            hypothesis = conn.execute("SELECT hypothesis_id,revision,status FROM research_hypotheses WHERE hypothesis_id=? AND research_id=? AND tenant_id=? AND user_id=? AND workspace_id=?", (hypothesis_id, research_id, tenant_id, user_id, workspace_id)).fetchone()
            if hypothesis is None:
                raise ValueError("hypothesis does not belong to project")
            if hypothesis["status"] not in {"approved", "testing"}:
                raise ValueError("hypothesis must be approved before creating an experiment")
            inherited_artifacts: list[dict[str, Any]] = []
            parent = None
            if parent_experiment_id:
                parent = conn.execute("SELECT * FROM research_experiments WHERE experiment_id=? AND research_id=?", (parent_experiment_id, research_id)).fetchone()
                if parent is None:
                    raise ValueError("parent experiment does not belong to project")
                if not parent["frozen_at"]:
                    raise ValueError("parent experiment must be completed and frozen before branching")
                inherited_artifacts = json.loads(str(parent["output_artifacts_json"] or "[]"))
            if execution_mode not in {"shared_workspace", "isolated_directory", "git_worktree"}:
                raise ValueError("invalid experiment execution mode")
            from .experiment_execution import ExperimentExecutionManager
            execution = ExperimentExecutionManager.prepare(
                research_id=research_id, experiment_id=experiment_id,
                mode=execution_mode, workspace=workdir,
                base_commit=str(parent["result_commit"] or parent["base_commit"] or "") if parent else "",
            )
            if workflow_run_id.strip():
                from ..workflow.manager import WorkflowRunManager
                linked_run = WorkflowRunManager.get(workflow_run_id.strip(), workdir=workdir)
                if linked_run is not None:
                    WorkflowRunManager.merge_input_payload(
                        linked_run.run_id,
                        metadata={"execution_workdir": execution["workdir"], "experiment_id": experiment_id},
                        workdir=workdir, expected_version=linked_run.version,
                    )
            snapshot = cls.workflow_run_snapshot(workflow_run_id, workdir)
            conn.execute(
                "INSERT INTO research_experiments(experiment_id,tenant_id,user_id,workspace_id,research_id,hypothesis_id,hypothesis_revision,workflow_run_id,parent_experiment_id,source_asset_id,source_experiment_id,title,reason,status,decision,result_summary,run_snapshot_json,execution_mode,execution_workdir,execution_manifest_json,input_artifacts_json,output_artifacts_json,metrics_json,base_commit,result_commit,created_by,created_at,updated_at,frozen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (experiment_id, tenant_id, user_id, workspace_id, research_id, hypothesis_id, int(hypothesis["revision"]), workflow_run_id.strip(), parent_experiment_id, source_asset_id.strip(), source_experiment_id.strip(), title.strip(), reason.strip(), "planned", "", "", canonical_json(snapshot), execution["mode"], execution["workdir"], canonical_json(execution), canonical_json(input_artifacts if input_artifacts is not None else inherited_artifacts), "[]", "{}", execution.get("base_commit", ""), "", created_by, now, now, ""),
            )
            conn.execute("UPDATE research_hypotheses SET status='testing',updated_at=? WHERE hypothesis_id=? AND status='approved'", (now, hypothesis_id))
            conn.commit()
            row = conn.execute("SELECT * FROM research_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        finally:
            conn.close()
        return ResearchExperiment.from_row(row)

    @classmethod
    def update_experiment(cls, experiment_id: str, *, tenant_id: str, user_id: str, workspace_id: str, status: str, decision: str = "", result_summary: str = "", workflow_run_id: str = "", workdir: str | None = None) -> ResearchExperiment:
        if status not in EXPERIMENT_STATUSES or decision not in EXPERIMENT_DECISIONS:
            raise ValueError("invalid experiment state")
        now = utc_now_iso()
        frozen_at = now if status in {"completed", "failed", "cancelled"} else ""
        conn = cls._connect(workdir)
        try:
            row = conn.execute("SELECT * FROM research_experiments WHERE experiment_id=? AND tenant_id=? AND user_id=? AND workspace_id=?", (experiment_id, tenant_id, user_id, workspace_id)).fetchone()
            if row is None:
                raise LookupError("experiment not found")
            if row["frozen_at"]:
                raise ValueError("completed experiment is frozen; create a child experiment")
            resolved_run_id = workflow_run_id.strip() or str(row["workflow_run_id"] or "")
            if resolved_run_id:
                from ..workflow.manager import WorkflowRunManager
                run = WorkflowRunManager.get(resolved_run_id, workdir=workdir)
                if run is not None and row["execution_workdir"]:
                    WorkflowRunManager.merge_input_payload(
                        resolved_run_id, metadata={"execution_workdir": row["execution_workdir"], "experiment_id": experiment_id}, workdir=workdir, expected_version=run.version,
                    )
            outputs = []
            metrics = {}
            result_commit = ""
            manifest = json.loads(str(row["execution_manifest_json"] or "{}"))
            if frozen_at:
                from .experiment_execution import ExperimentExecutionManager
                frozen = ExperimentExecutionManager.freeze(manifest)
                manifest = {**manifest, **frozen}
                result_commit = str(frozen.get("result_commit") or row["base_commit"] or "")
            snapshot = cls.workflow_run_snapshot(resolved_run_id, workdir) if frozen_at else {}
            if frozen_at:
                for stage in snapshot.get("stages") or []:
                    outputs.extend(cls._artifact_record(name, value, stage_id=str(stage.get("stage_id") or ""), execution_workdir=str(row["execution_workdir"] or "")) for name, value in dict(stage.get("artifacts") or {}).items())
                    validation = dict(stage.get("validation") or {})
                    if validation: metrics[str(stage.get("stage_id") or "stage")] = validation
            conn.execute("UPDATE research_experiments SET status=?,decision=?,result_summary=?,workflow_run_id=?,run_snapshot_json=CASE WHEN ?='' THEN run_snapshot_json ELSE ? END,execution_manifest_json=?,output_artifacts_json=?,metrics_json=?,result_commit=?,updated_at=?,frozen_at=? WHERE experiment_id=?", (status, decision, result_summary.strip(), resolved_run_id, canonical_json(snapshot), canonical_json(snapshot), canonical_json(manifest), canonical_json(outputs), canonical_json(metrics), result_commit, now, frozen_at, experiment_id))
            conn.commit()
            updated = conn.execute("SELECT * FROM research_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        finally:
            conn.close()
        return ResearchExperiment.from_row(updated)
