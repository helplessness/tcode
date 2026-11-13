"""Artifact helpers for multi-step task execution."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ── Structured intermediate artifact conventions ─────────────

class ArtifactConvention:
    """Naming and structure conventions for intermediate artifacts.

    These conventions enable workspace-as-cache patterns: downstream agents
    can reliably find and consume intermediate outputs from upstream agents
    without guessing file paths.
    """

    # Directory layout within a workspace
    DIRS = {
        "research": "research",        # Literature, analysis summaries
        "design": "design",            # Design specifications, schemas
        "implementation": "src",       # Code, scripts
        "validation": "validation",    # Test results, verification reports
        "output": "output",            # Final deliverables
        "temp": "tmp",                 # Temporary scratch files
    }

    # Standard artifact filenames
    FILES = {
        "plan": "plan.json",                      # Structured plan
        "summary": "summary.md",                  # Human-readable summary
        "contract": "contract.json",              # Input/output contract
        "result": "result.json",                  # Execution result
        "candidates": "candidates.json",          # Candidate designs/selections
        "scores": "scores.json",                  # Scoring/ranking results
        "validation_report": "validation.md",     # Validation report
        "review": "review.json",                  # Peer review output
        "tool_trace": "tool_trace.jsonl",         # Tool execution log
    }

    @classmethod
    def artifact_path(
        cls,
        workspace: str | Path,
        category: str,
        artifact_type: str,
        *,
        stage_id: str = "",
    ) -> Path:
        """Build a conventional path for an intermediate artifact.

        Example:
            ArtifactConvention.artifact_path("/ws/ISSUE-1", "research", "summary")
            → /ws/ISSUE-1/research/summary.md
        """
        base = Path(workspace)
        subdir = cls.DIRS.get(category, category)
        filename = cls.FILES.get(artifact_type, f"{artifact_type}.json")
        if stage_id:
            return base / subdir / str(stage_id) / filename
        return base / subdir / filename

    @classmethod
    def write_artifact(
        cls,
        workspace: str | Path,
        category: str,
        artifact_type: str,
        content: str | dict[str, Any],
        *,
        stage_id: str = "",
    ) -> Path:
        """Write an artifact following conventions. Returns the written path."""
        path = cls.artifact_path(workspace, category, artifact_type, stage_id=stage_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, dict):
            path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.write_text(str(content), encoding="utf-8")
        return path

    @classmethod
    def read_artifact(
        cls,
        workspace: str | Path,
        category: str,
        artifact_type: str,
        *,
        stage_id: str = "",
    ) -> Optional[str]:
        """Read an artifact if it exists. Returns None if missing."""
        path = cls.artifact_path(workspace, category, artifact_type, stage_id=stage_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    @classmethod
    def list_artifacts(cls, workspace: str | Path) -> dict[str, list[str]]:
        """List all convention artifacts in a workspace, grouped by category."""
        base = Path(workspace)
        result: dict[str, list[str]] = {}
        for category, subdir in cls.DIRS.items():
            cat_path = base / subdir
            if not cat_path.exists():
                continue
            files: list[str] = []
            for p in cat_path.rglob("*"):
                if p.is_file():
                    files.append(str(p.relative_to(base)))
            if files:
                result[category] = sorted(files)
        return result


class RuntimeArtifactManager:
    _TOOL_LOG_LOCK = threading.RLock()

    @classmethod
    def _base_dir(cls, workdir: Optional[str] = None) -> Path:
        base = workdir or os.environ.get("TCODE_WORKDIR", os.getcwd())
        return Path(base) / ".tcode" / "artifacts"

    @classmethod
    def _tool_log_base_dir(cls, workdir: Optional[str] = None) -> Path:
        base = workdir or os.environ.get("TCODE_WORKDIR", os.getcwd())
        return Path(base) / ".tcode" / "debug" / "tool_logs"

    @classmethod
    def _workflow_log_base_dir(cls, workdir: Optional[str] = None) -> Path:
        base = workdir or os.environ.get("TCODE_WORKDIR", os.getcwd())
        return Path(base) / ".tcode" / "workflow" / "logs"

    @classmethod
    def _safe_name(cls, value: str, fallback: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
        return text or fallback

    @classmethod
    def _tool_log_text_limit(cls) -> int:
        try:
            return max(500, int(os.environ.get("TCODE_TOOL_LOG_TEXT_LIMIT", "12000") or "12000"))
        except Exception:
            return 12000

    @classmethod
    def _tool_log_list_limit(cls) -> int:
        try:
            return max(5, int(os.environ.get("TCODE_TOOL_LOG_LIST_LIMIT", "40") or "40"))
        except Exception:
            return 40

    @classmethod
    def _tool_log_dict_limit(cls) -> int:
        try:
            return max(5, int(os.environ.get("TCODE_TOOL_LOG_DICT_LIMIT", "60") or "60"))
        except Exception:
            return 60

    @classmethod
    def _tool_log_depth_limit(cls) -> int:
        try:
            return max(1, int(os.environ.get("TCODE_TOOL_LOG_DEPTH_LIMIT", "5") or "5"))
        except Exception:
            return 5

    @classmethod
    def _truncate_text(cls, value: Any, *, limit: Optional[int] = None) -> str:
        text = str(value or "")
        max_chars = max(80, int(limit or cls._tool_log_text_limit()))
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 24] + " ...[truncated]"

    @classmethod
    def _sanitize_log_value(cls, value: Any, *, depth: int = 0) -> Any:
        from .telemetry import redact_telemetry_value

        return redact_telemetry_value(
            value,
            depth=depth,
            text_limit=cls._tool_log_text_limit(),
            list_limit=cls._tool_log_list_limit(),
            dict_limit=cls._tool_log_dict_limit(),
            depth_limit=cls._tool_log_depth_limit(),
        )

    @classmethod
    def tool_execution_log_paths(
        cls,
        *,
        session_id: str,
        workdir: Optional[str] = None,
        workflow_run_id: str = "",
        workflow_stage_id: str = "",
    ) -> dict[str, str]:
        safe_session = cls._safe_name(session_id, "session")
        session_path = cls._tool_log_base_dir(workdir) / "sessions" / f"{safe_session}.jsonl"
        paths = {
            "session_log_jsonl": str(session_path),
        }
        normalized_run_id = str(workflow_run_id or "").strip()
        if normalized_run_id:
            safe_run = cls._safe_name(normalized_run_id, "run")
            run_dir = cls._workflow_log_base_dir(workdir) / safe_run
            paths["workflow_run_log_jsonl"] = str(run_dir / "tool_calls.jsonl")
            normalized_stage_id = str(workflow_stage_id or "").strip()
            if normalized_stage_id:
                safe_stage = cls._safe_name(normalized_stage_id, "stage")
                paths["workflow_stage_log_jsonl"] = str(
                    run_dir / "stages" / f"{safe_stage}.jsonl"
                )
        return paths

    @classmethod
    def _append_jsonl(cls, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @classmethod
    def append_tool_execution_log(
        cls,
        *,
        session_id: str,
        agent_name: str,
        tool_name: str,
        call_id: str = "",
        arguments: Any = None,
        result: Optional[dict[str, Any]] = None,
        state: str = "",
        source: str = "",
        workdir: Optional[str] = None,
        workflow_run_id: str = "",
        workflow_stage_id: str = "",
        parent_session_id: str = "",
        subagent_session_id: str = "",
        workflow_template_key: str = "",
        workflow_owner_session_id: str = "",
        worker_name: str = "",
        mcp_server: str = "",
        mcp_tool_name: str = "",
        turn_message_id: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> dict[str, str]:
        duration_ms = None
        if isinstance(start_time, int) and isinstance(end_time, int):
            duration_ms = max(0, end_time - start_time)
        from .computer import computer_operation, is_computer_tool
        if is_computer_tool(tool_name):
            # Computer observations and inputs can contain screen text, paths,
            # coordinates and typed content.  Persist only the broker's fixed
            # audit projection, including exception and timeout paths.
            metadata = dict((result or {}).get("metadata") or {}) if isinstance(result, dict) else {}
            projection = dict(metadata.get("computer_audit") or {})
            arguments = {"operation": computer_operation(tool_name, arguments) or "unknown"}
            result = {"metadata": {"computer_audit": projection}}
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": str(session_id or "").strip(),
            "parent_session_id": str(parent_session_id or "").strip(),
            "subagent_session_id": str(subagent_session_id or "").strip(),
            "agent_name": str(agent_name or "").strip(),
            "turn_message_id": str(turn_message_id or "").strip(),
            "tool_name": str(tool_name or "").strip(),
            "call_id": str(call_id or "").strip(),
            "state": str(state or "").strip(),
            "source": str(source or "").strip(),
            "arguments": cls._sanitize_log_value(arguments or {}),
            "result": cls._sanitize_log_value(result or {}),
            "workflow_run_id": str(workflow_run_id or "").strip(),
            "workflow_stage_id": str(workflow_stage_id or "").strip(),
            "workflow_template_key": str(workflow_template_key or "").strip(),
            "workflow_owner_session_id": str(workflow_owner_session_id or "").strip(),
            "worker_name": str(worker_name or "").strip(),
            "mcp_server": str(mcp_server or "").strip(),
            "mcp_tool_name": str(mcp_tool_name or "").strip(),
            "start_time_ms": start_time,
            "end_time_ms": end_time,
            "duration_ms": duration_ms,
        }
        paths = cls.tool_execution_log_paths(
            session_id=session_id,
            workdir=workdir,
            workflow_run_id=workflow_run_id,
            workflow_stage_id=workflow_stage_id,
        )
        with cls._TOOL_LOG_LOCK:
            cls._append_jsonl(Path(paths["session_log_jsonl"]), payload)
            run_log = str(paths.get("workflow_run_log_jsonl") or "").strip()
            if run_log:
                cls._append_jsonl(Path(run_log), payload)
            stage_log = str(paths.get("workflow_stage_log_jsonl") or "").strip()
            if stage_log:
                cls._append_jsonl(Path(stage_log), payload)
        return paths

    @classmethod
    def task_dir(cls, session_id: str, task_id: str, workdir: Optional[str] = None) -> Path:
        path = cls._base_dir(workdir) / str(session_id) / str(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def write_bundle(
        cls,
        *,
        session_id: str,
        task_id: str,
        workdir: Optional[str] = None,
        contract: Optional[dict[str, Any]] = None,
        summary: str = "",
        result: Optional[dict[str, Any]] = None,
        tool_trace: Optional[list[dict[str, Any]]] = None,
        full_result: Optional[str] = None,
    ) -> dict[str, str]:
        task_dir = cls.task_dir(session_id, task_id, workdir)
        paths = {
            "contract_json": str(task_dir / "contract.json"),
            "summary_md": str(task_dir / "summary.md"),
            "result_json": str(task_dir / "result.json"),
            "tool_trace_json": str(task_dir / "tool_trace.json"),
        }
        (task_dir / "contract.json").write_text(
            json.dumps(contract or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (task_dir / "summary.md").write_text(str(summary or ""), encoding="utf-8")
        (task_dir / "result.json").write_text(
            json.dumps(result or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (task_dir / "tool_trace.json").write_text(
            json.dumps(tool_trace or [], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if full_result is not None:
            full_result_text = str(full_result)
            full_result_name = "full_result.md"
            try:
                json.loads(full_result_text)
                full_result_name = "full_result.json"
            except Exception:
                pass
            full_result_path = task_dir / full_result_name
            full_result_path.write_text(full_result_text, encoding="utf-8")
            paths["full_result_path"] = str(full_result_path)
        return paths

    @classmethod
    def list_for_session(cls, session_id: str, workdir: Optional[str] = None) -> list[dict[str, Any]]:
        base = cls._base_dir(workdir) / str(session_id)
        if not base.exists():
            return []
        items: list[dict[str, Any]] = []
        for task_dir in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not task_dir.is_dir():
                continue
            item: dict[str, Any] = {"task_id": task_dir.name, "path": str(task_dir)}
            contract_path = task_dir / "contract.json"
            result_path = task_dir / "result.json"
            summary_path = task_dir / "summary.md"
            if contract_path.exists():
                try:
                    contract = json.loads(contract_path.read_text(encoding="utf-8"))
                except Exception:
                    contract = {}
                if isinstance(contract, dict):
                    item["contract"] = {
                        "goal": str(contract.get("goal", "") or ""),
                        "deliverable": str(contract.get("deliverable", "") or ""),
                        "stop_when": str(contract.get("stop_when", "") or ""),
                    }
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception:
                    result = {}
                if isinstance(result, dict):
                    item["result"] = {
                        "status": str(result.get("status", "") or ""),
                        "review_verdict": str(result.get("review_verdict", "") or ""),
                        "next_action": str(result.get("next_action", "") or ""),
                    }
            if summary_path.exists():
                item["summary_preview"] = summary_path.read_text(encoding="utf-8")[:300]
            item["files"] = [
                str(path)
                for path in sorted(task_dir.iterdir())
                if path.is_file()
            ]
            items.append(item)
        return items[:20]
