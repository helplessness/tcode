"""Strategy-backed execution workspaces for subagents and team workers.

The control plane (sessions, reviews, runtime metadata) remains rooted in the
original workspace. Protein-domain work uses scientific run directories with
read-only inputs and indexed outputs. Git worktrees remain available for
software-maintenance tasks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_TERMINAL_OUTCOMES = {"completed", "failed", "cancelled", "blocked"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def _workspace_id(
    kind: str,
    task_id: str,
    control_workdir: Path,
    strategy: str,
) -> str:
    prefix = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "-",
        f"{kind or 'subagent'}-{task_id or 'task'}",
    ).strip("-.")[:64] or "task"
    identity = f"{strategy}:{kind}:{task_id}:{control_workdir}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


@dataclass
class AgentWorkspace:
    """Serializable workspace transaction record."""

    workspace_id: str
    task_id: str
    session_id: str
    kind: str
    control_workdir: str
    repo_root: str
    worktree_root: str
    execution_workdir: str
    strategy: str = "git_worktree"
    domain: str = ""
    agent_name: str = ""
    worker_name: str = ""
    workspace_root: str = ""
    inputs_dir: str = ""
    outputs_dir: str = ""
    logs_dir: str = ""
    manifest_path: str = ""
    resource_context: dict[str, Any] = field(default_factory=dict)
    input_resources: list[dict[str, Any]] = field(default_factory=list)
    output_artifacts: list[dict[str, Any]] = field(default_factory=list)
    candidate_registry: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    checkpoint_state: dict[str, Any] = field(default_factory=dict)
    resume_count: int = 0
    task_contract: dict[str, Any] = field(default_factory=dict)
    base_ref: str = "HEAD"
    base_sha: str = ""
    isolated: bool = True
    status: str = "active"
    outcome: str = ""
    dirty: bool = False
    changed_files: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    finalized_at: str = ""
    cleaned_at: str = ""
    retained_reason: str = ""
    fallback_reason: str = ""
    error: str = ""

    @property
    def execution_path(self) -> Path:
        return Path(self.execution_workdir)

    @property
    def worktree_path(self) -> Path:
        return Path(self.workspace_root or self.worktree_root)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentWorkspace":
        known = cls.__dataclass_fields__
        values = {key: value for key, value in dict(payload or {}).items() if key in known}
        values["changed_files"] = [
            str(item) for item in (values.get("changed_files") or []) if str(item)
        ]
        values["input_resources"] = [
            dict(item)
            for item in (values.get("input_resources") or [])
            if isinstance(item, dict)
        ]
        values["output_artifacts"] = [
            dict(item)
            for item in (values.get("output_artifacts") or [])
            if isinstance(item, dict)
        ]
        values["resource_context"] = (
            dict(values.get("resource_context") or {})
            if isinstance(values.get("resource_context"), dict)
            else {}
        )
        values["task_contract"] = (
            dict(values.get("task_contract") or {})
            if isinstance(values.get("task_contract"), dict)
            else {}
        )
        for key in ("candidate_registry", "provenance", "checkpoint_state"):
            values[key] = (
                dict(values.get(key) or {})
                if isinstance(values.get(key), dict)
                else {}
            )
        try:
            values["resume_count"] = max(0, int(values.get("resume_count") or 0))
        except (TypeError, ValueError):
            values["resume_count"] = 0
        values.setdefault(
            "strategy",
            "git_worktree" if str(values.get("repo_root") or "") else "shared",
        )
        values.setdefault("workspace_root", str(values.get("worktree_root") or ""))
        return cls(**values)


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    removed: bool
    retained: bool
    reason: str
    workspace: AgentWorkspace


class AgentWorkspaceError(RuntimeError):
    """Raised when an enabled workspace cannot be isolated safely."""


class AgentWorkspaceManager:
    """Create, inspect, finalize and safely remove delegated workspaces."""

    _LOCK = threading.RLock()

    @classmethod
    def workspace_id_for(
        cls, *, control_workdir: str | Path, kind: str, task_id: str,
        strategy: str,
    ) -> str:
        control = Path(control_workdir).expanduser().resolve(strict=False)
        return _workspace_id(kind, task_id, control, strategy)

    @classmethod
    def enabled(cls) -> bool:
        from ..runtime.sandbox.base import strict_isolation_enabled

        if strict_isolation_enabled():
            return True
        return _env_bool("TCODE_AGENT_WORKSPACE_ENABLED", True)

    @classmethod
    def resolve_strategy(
        cls,
        *,
        control_workdir: str | Path,
        requested: str = "auto",
        domain: str = "",
        agent_name: str = "",
        worker_name: str = "",
        resource_context: Optional[dict[str, Any]] = None,
    ) -> str:
        """Resolve the execution backend without inferring from free-form prompts."""

        configured = str(os.environ.get("TCODE_AGENT_WORKSPACE_STRATEGY", "") or "").strip()
        requested_value = str(requested or "auto").strip().lower()
        value = configured or requested_value
        aliases = {
            "protein": "protein_run",
            "run": "protein_run",
            "scientific_run": "protein_run",
            "git": "git_worktree",
            "worktree": "git_worktree",
            "none": "shared",
            "disabled": "shared",
        }
        value = aliases.get(value, value)
        from ..runtime.sandbox.base import strict_isolation_enabled

        if strict_isolation_enabled():
            if requested_value == "snapshot":
                return "snapshot"
            if value == "shared":
                raise AgentWorkspaceError("strict isolation refuses a shared agent workspace")
            if value == "protein_run":
                return value
            if value == "snapshot":
                return value
            control = Path(control_workdir or os.getcwd()).expanduser().resolve(strict=False)
            return "git_worktree" if cls._repo_root(control) is not None else "snapshot"
        if value in {"protein_run", "git_worktree", "snapshot", "shared"}:
            return value

        normalized_domain = str(domain or "").strip().lower()
        normalized_agent = str(agent_name or "").strip().lower()
        normalized_worker = str(worker_name or "").strip().lower()
        if (
            normalized_domain == "protein"
            or normalized_agent.startswith("protein_")
            or normalized_agent == "protein_orchestrator"
        ):
            return "protein_run"

        # ``_biosafety`` is governance metadata shared by every team task.  It
        # intentionally does not select an execution backend; only an explicit
        # domain/strategy or a protein execution agent does that.

        # Paper agents operate on runtime research state (uploaded PDFs, the
        # local paper registry and generated evidence), not on a committed
        # source snapshot. A detached Git worktree cannot see untracked upload
        # files and would write library updates outside the control workspace.
        # Keep their automatic execution in the canonical research workspace;
        # callers can still explicitly request another strategy when needed.
        if (
            normalized_domain == "paper"
            or normalized_agent.startswith(("paper_", "worker_paper_"))
            or normalized_worker.startswith("paper_")
        ):
            return "shared"

        control = Path(control_workdir or os.getcwd()).expanduser().resolve(strict=False)
        return "git_worktree" if cls._repo_root(control) is not None else "shared"

    @classmethod
    def _run_git(
        cls,
        cwd: Path,
        *args: str,
        check: bool = True,
        input_text: Optional[str] = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    @classmethod
    def _git_text(cls, cwd: Path, *args: str) -> str:
        return cls._run_git(cwd, *args).stdout.strip()

    @classmethod
    def _repo_root(cls, workdir: Path) -> Optional[Path]:
        try:
            value = cls._git_text(workdir, "rev-parse", "--show-toplevel")
        except (OSError, subprocess.SubprocessError):
            return None
        return Path(value).expanduser().resolve(strict=False) if value else None

    @classmethod
    def _git_common_dir(cls, repo_root: Path) -> Path:
        value = cls._git_text(repo_root, "rev-parse", "--git-common-dir")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = repo_root / path
        return path.resolve(strict=False)

    @classmethod
    def _workspace_root(cls, repo_root: Path) -> Path:
        configured = str(os.environ.get("TCODE_AGENT_WORKSPACE_ROOT", "") or "").strip()
        root = (
            Path(configured).expanduser()
            if configured
            else cls._git_common_dir(repo_root) / "tcode" / "agent-worktrees"
        )
        if not root.is_absolute():
            root = repo_root / root
        return root.resolve(strict=False)

    @staticmethod
    def _snapshot_root(control: Path) -> Path:
        return control / ".tcode" / "agent-snapshots"

    @staticmethod
    def _snapshot_files(root: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in root.rglob("*"):
            if path.is_symlink():
                raise AgentWorkspaceError("strict snapshot refuses symbolic links")
            if not path.is_file():
                continue
            if len(files) >= 5000:
                raise AgentWorkspaceError("strict snapshot exceeds 5000 files")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            files[path.relative_to(root).as_posix()] = digest.hexdigest()
        return files

    @classmethod
    def _metadata_root(cls, control_workdir: Path) -> Path:
        repo_root = cls._repo_root(control_workdir)
        if repo_root is not None:
            try:
                return cls._git_common_dir(repo_root) / "tcode" / "agent-workspaces"
            except (OSError, subprocess.SubprocessError):
                pass
        return control_workdir / ".tcode" / "agent-workspaces"

    @classmethod
    def _metadata_path(cls, control_workdir: Path, workspace_id: str) -> Path:
        return cls._metadata_root(control_workdir) / f"{workspace_id}.json"

    @classmethod
    def _write_metadata(cls, workspace: AgentWorkspace) -> None:
        from ..runtime.sandbox.execution_scope import register_workspace

        register_workspace(workspace)
        path = cls._metadata_path(Path(workspace.control_workdir), workspace.workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(workspace.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @classmethod
    def _prepare_git_uploaded_resources(
        cls,
        *,
        workspace: AgentWorkspace,
        resource_context: Optional[dict[str, Any]],
    ) -> None:
        """Copy declared uploads into a Git worktree at stable workspace paths.

        A delegated worktree cannot see untracked files from its control
        workspace.  Preserve each upload's workspace-relative path (for
        example ``upload/paper.pdf``) and rewrite the resource context to the
        isolated host path.  This keeps the public sandbox path
        ``/workspace/upload/paper.pdf`` valid for every agent type.
        """

        from ..runtime.resource_import import (
            attachment_kind,
            container_path_for_workspace_relative,
            env_int,
            iter_resource_descriptors,
            path_tree_size,
            resolve_resource_source,
        )

        original = (
            dict(resource_context or {})
            if isinstance(resource_context, dict)
            else {}
        )
        control = Path(workspace.control_workdir).resolve(strict=False)
        execution = workspace.execution_path.resolve(strict=False)
        worktree_root = workspace.worktree_path.resolve(strict=False)
        max_bytes = max(
            1,
            env_int(
                "TCODE_AGENT_WORKSPACE_INPUT_COPY_MAX_BYTES",
                500 * 1024 * 1024,
            ),
        )
        max_files = max(
            1,
            env_int("TCODE_AGENT_WORKSPACE_INPUT_COPY_MAX_FILES", 2048),
        )

        declared_items: list[dict[str, Any]] = []
        for item in list(original.get("uploaded_files") or []):
            if isinstance(item, dict):
                declared_items.append(item)
        paper_inputs = original.get("paper_inputs")
        if isinstance(paper_inputs, dict):
            for item in list(paper_inputs.get("inputs") or []):
                if isinstance(item, dict):
                    declared_items.append(item)
        # ``assign_task.resources`` and plugin-defined resource payloads are
        # intentionally schema-open.  Discover every path-bearing descriptor
        # instead of recognizing only uploads and paper-specific records.
        for item in iter_resource_descriptors(original):
            if isinstance(item, dict):
                declared_items.append(item)

        records_by_source: dict[str, dict[str, Any]] = {}
        for item in declared_items:
            resolved_candidates: list[Path] = []
            for key in (
                "path",
                "mount_target_path",
                "container_path",
                "relative_path",
                "source_path",
            ):
                raw = str(item.get(key) or "").strip()
                if not raw:
                    continue
                try:
                    candidate = resolve_resource_source(raw, control)
                except Exception:
                    continue
                if candidate.is_file() or candidate.is_dir():
                    resolved = candidate.resolve(strict=False)
                    if resolved not in resolved_candidates:
                        resolved_candidates.append(resolved)
            external_mount = bool(item.get("mounted")) and not bool(
                item.get("imported")
            )
            if external_mount:
                raw_source = str(item.get("source_path") or "").strip()
                try:
                    source = (
                        resolve_resource_source(raw_source, control)
                        if raw_source
                        else None
                    )
                except Exception:
                    source = None
            else:
                source = next(
                    (
                        candidate
                        for candidate in resolved_candidates
                        if _inside(candidate, control)
                    ),
                    resolved_candidates[0] if resolved_candidates else None,
                )
            if source is None:
                continue
            source_key = source.as_posix()
            if source_key in records_by_source:
                continue
            try:
                relative = source.relative_to(control)
            except ValueError:
                raise AgentWorkspaceError(
                    "delegated resource must be imported into the control workspace "
                    f"before isolation: {source}"
                )
            if not relative.parts or relative.parts[0] == ".git":
                raise AgentWorkspaceError(f"unsafe delegated resource path: {source}")

            target = (execution / relative).resolve(strict=False)
            if not _inside(target, worktree_root):
                raise AgentWorkspaceError(f"delegated resource escapes worktree: {source}")
            file_count, total_bytes = path_tree_size(
                source,
                max_files=max_files + 1,
            )
            if file_count > max_files or total_bytes > max_bytes:
                raise AgentWorkspaceError(
                    "delegated resource exceeds isolated-copy limits: "
                    f"{source} ({file_count} files, {total_bytes} bytes)"
                )

            snapshot_created = not target.exists()
            if snapshot_created:
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=False)
                else:
                    shutil.copy2(source, target)
                try:
                    if target.is_dir():
                        for child in target.rglob("*"):
                            child.chmod(0o555 if child.is_dir() else 0o444)
                        target.chmod(0o555)
                    else:
                        target.chmod(0o444)
                except OSError:
                    pass

            relative_text = relative.as_posix()
            records_by_source[source_key] = {
                "name": source.name,
                "kind": attachment_kind(source),
                "source_path": source_key,
                "path": target.as_posix(),
                "relative_path": relative_text,
                "worktree_relative_path": target.relative_to(worktree_root).as_posix(),
                "container_path": container_path_for_workspace_relative(relative_text),
                "mount_target_path": target.as_posix(),
                "mode": "snapshot",
                "import_mode": "snapshot",
                "snapshot": True,
                "snapshot_created": snapshot_created,
                "mounted": False,
                "read_only": True,
                "available_in_sandbox": True,
                "file_count": file_count,
                "size": total_bytes,
            }

        def _mapped_item(item: Any) -> Any:
            if not isinstance(item, dict):
                return item
            mapped = dict(item)
            for key in (
                "path",
                "mount_target_path",
                "container_path",
                "relative_path",
                "source_path",
            ):
                raw = str(item.get(key) or "").strip()
                if not raw:
                    continue
                try:
                    source = resolve_resource_source(raw, control).resolve(strict=False)
                except Exception:
                    continue
                record = records_by_source.get(source.as_posix())
                if record is not None:
                    mapped.update(
                        {
                            "path": record["path"],
                            "source_path": record["source_path"],
                            "relative_path": record["relative_path"],
                            "container_path": record["container_path"],
                            "mount_target_path": record["mount_target_path"],
                            "available_in_sandbox": True,
                            "imported": True,
                            "mounted": False,
                            "read_only": True,
                            "import_mode": "snapshot",
                            "workspace_input": True,
                        }
                    )
                    break
            return mapped

        def _mapped_value(value: Any, *, depth: int = 0) -> Any:
            if depth > 8:
                return value
            if isinstance(value, dict):
                nested = {
                    key: _mapped_value(item, depth=depth + 1)
                    for key, item in value.items()
                }
                return _mapped_item(nested)
            if isinstance(value, list):
                return [
                    _mapped_value(item, depth=depth + 1)
                    for item in value
                ]
            return value

        mapped_context = _mapped_value(original)
        workspace_marker = dict(mapped_context.get("_workspace") or {})
        workspace_marker.update(
            {
                "strategy": "git_worktree",
                "control_workdir": control.as_posix(),
                "execution_workdir": execution.as_posix(),
                "read_only_paths": [
                    record["path"] for record in records_by_source.values()
                ],
            }
        )
        mapped_context["_workspace"] = workspace_marker
        workspace.resource_context = mapped_context
        workspace.input_resources = list(records_by_source.values())

    @classmethod
    def _remove_git_uploaded_resources(cls, workspace: AgentWorkspace) -> None:
        """Remove only input snapshots created by this workspace manager."""

        root = workspace.worktree_path.resolve(strict=False)
        for record in list(workspace.input_resources or []):
            if not bool(record.get("snapshot_created")):
                continue
            raw = str(record.get("path") or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser().resolve(strict=False)
            if not _inside(path, root) or path == root or not path.exists():
                continue
            try:
                if path.is_dir():
                    for child in path.rglob("*"):
                        try:
                            child.chmod(0o755 if child.is_dir() else 0o644)
                        except OSError:
                            pass
                    path.chmod(0o755)
                    shutil.rmtree(path)
                else:
                    path.chmod(0o644)
                    path.unlink()
            except OSError:
                continue

    @classmethod
    def load(
        cls,
        *,
        control_workdir: str | Path,
        workspace_id: str,
    ) -> Optional[AgentWorkspace]:
        path = cls._metadata_path(
            Path(control_workdir).expanduser().resolve(strict=False),
            workspace_id,
        )
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return AgentWorkspace.from_dict(payload) if isinstance(payload, dict) else None
        except (OSError, TypeError, ValueError):
            return None

    @classmethod
    def _refresh_persisted_protein_state(
        cls,
        workspace: AgentWorkspace,
    ) -> AgentWorkspace:
        """Merge async watcher state into a caller's in-memory workspace."""

        if workspace.strategy != "protein_run":
            return workspace
        latest = cls.load(
            control_workdir=workspace.control_workdir,
            workspace_id=workspace.workspace_id,
        )
        if latest is not None and latest is not workspace:
            workspace.provenance = dict(latest.provenance or workspace.provenance or {})
            workspace.checkpoint_state = dict(
                latest.checkpoint_state or workspace.checkpoint_state or {}
            )
            workspace.output_artifacts = list(
                latest.output_artifacts or workspace.output_artifacts or []
            )
            workspace.candidate_registry = dict(
                latest.candidate_registry or workspace.candidate_registry or {}
            )
            workspace.status = str(latest.status or workspace.status)
            workspace.outcome = str(latest.outcome or workspace.outcome)
            workspace.finalized_at = str(latest.finalized_at or workspace.finalized_at)
            workspace.retained_reason = str(
                latest.retained_reason or workspace.retained_reason
            )
            workspace.updated_at = str(latest.updated_at or workspace.updated_at)
        from .protein_run import hydrate_persisted_state

        hydrate_persisted_state(workspace)
        return workspace

    @classmethod
    def _fallback(
        cls,
        *,
        control_workdir: Path,
        task_id: str,
        session_id: str,
        kind: str,
        reason: str,
        agent_name: str = "",
        worker_name: str = "",
        domain: str = "",
        resource_context: Optional[dict[str, Any]] = None,
        task_contract: Optional[dict[str, Any]] = None,
    ) -> AgentWorkspace:
        workspace_id = _workspace_id(
            kind,
            task_id or session_id,
            control_workdir,
            "shared",
        )
        workspace = AgentWorkspace(
            workspace_id=workspace_id,
            task_id=str(task_id or ""),
            session_id=str(session_id or ""),
            kind=str(kind or "subagent"),
            control_workdir=str(control_workdir),
            repo_root="",
            worktree_root=str(control_workdir),
            execution_workdir=str(control_workdir),
            strategy="shared",
            domain=str(domain or ""),
            agent_name=str(agent_name or ""),
            worker_name=str(worker_name or ""),
            workspace_root=str(control_workdir),
            resource_context=dict(resource_context or {}),
            task_contract=dict(task_contract or {}),
            isolated=False,
            status="fallback",
            fallback_reason=reason,
        )
        cls._write_metadata(workspace)
        return workspace

    @classmethod
    def create(
        cls,
        *,
        control_workdir: str | Path,
        task_id: str,
        session_id: str = "",
        kind: str = "subagent",
        base_ref: str = "HEAD",
        strategy: str = "auto",
        domain: str = "",
        agent_name: str = "",
        worker_name: str = "",
        resource_context: Optional[dict[str, Any]] = None,
        task_contract: Optional[dict[str, Any]] = None,
        copy_control_changes: bool = False,
    ) -> AgentWorkspace:
        """Create or resume an isolated workspace for one delegated task.

        Protein runs snapshot/mount declared resources. Git worktrees snapshot
        declared uploads at their stable workspace-relative paths without
        staging, stashing, committing, resetting, or otherwise mutating the
        caller's working tree.
        """

        control = Path(control_workdir or os.getcwd()).expanduser().resolve(strict=False)
        resolved_strategy = cls.resolve_strategy(
            control_workdir=control,
            requested=strategy,
            domain=domain,
            agent_name=agent_name,
            worker_name=worker_name,
            resource_context=resource_context,
        )
        if not cls.enabled():
            resolved_strategy = "shared"
        workspace_id = _workspace_id(
            kind,
            task_id or session_id,
            control,
            resolved_strategy,
        )
        with cls._LOCK:
            existing = cls.load(control_workdir=control, workspace_id=workspace_id)
            if (
                existing is not None
                and existing.isolated
                and existing.status != "cleaned"
                and existing.worktree_path.is_dir()
            ):
                existing.status = "active"
                existing.updated_at = _utc_now()
                if existing.strategy == "protein_run":
                    from .protein_run import resume_run

                    existing.task_contract = dict(task_contract or existing.task_contract or {})
                    try:
                        resume_run(existing)
                    except Exception as exc:
                        existing.status = "retained"
                        existing.retained_reason = "input_drift"
                        existing.error = str(exc)[:500]
                        existing.updated_at = _utc_now()
                        cls._write_metadata(existing)
                        raise AgentWorkspaceError(
                            f"failed to resume protein run workspace: {exc}"
                        ) from exc
                cls._write_metadata(existing)
                return existing

            if resolved_strategy == "shared":
                auto_without_git = (
                    str(strategy or "auto").strip().lower() == "auto"
                    and cls._repo_root(control) is None
                )
                return cls._fallback(
                    control_workdir=control,
                    task_id=task_id,
                    session_id=session_id,
                    kind=kind,
                    reason=(
                        "agent_workspace_disabled"
                        if not cls.enabled()
                        else (
                            "not_a_git_repository"
                            if auto_without_git
                            else "shared_workspace_requested"
                        )
                    ),
                    agent_name=agent_name,
                    worker_name=worker_name,
                    domain=domain,
                    resource_context=resource_context,
                    task_contract=task_contract,
                )

            if resolved_strategy == "snapshot":
                root = cls._snapshot_root(control).resolve(strict=False)
                destination = (root / workspace_id).resolve(strict=False)
                if not _inside(destination, root) or destination == root:
                    raise AgentWorkspaceError("unsafe agent snapshot path")
                root.mkdir(parents=True, exist_ok=True)
                try:
                    ignored = {".tcode", ".tcode-local", ".git", ".env"}
                    for parent, directories, files in os.walk(control, followlinks=False):
                        directories[:] = [name for name in directories if name not in ignored]
                        for name in (*directories, *files):
                            if name not in ignored and (Path(parent) / name).is_symlink():
                                raise AgentWorkspaceError("strict snapshot refuses symbolic links")
                    shutil.copytree(
                        control,
                        destination,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(*ignored),
                    )
                    baseline = cls._snapshot_files(destination)
                except Exception as exc:
                    shutil.rmtree(destination, ignore_errors=True)
                    raise AgentWorkspaceError(f"failed to create isolated snapshot: {exc}") from exc
                workspace = AgentWorkspace(
                    workspace_id=workspace_id,
                    task_id=str(task_id or ""),
                    session_id=str(session_id or ""),
                    kind=str(kind or "subagent"),
                    control_workdir=str(control),
                    repo_root="",
                    worktree_root=str(destination),
                    execution_workdir=str(destination),
                    strategy="snapshot",
                    domain=str(domain or ""),
                    agent_name=str(agent_name or ""),
                    worker_name=str(worker_name or ""),
                    workspace_root=str(destination),
                    resource_context=dict(resource_context or {}),
                    task_contract=dict(task_contract or {}),
                    provenance={"snapshot_baseline": baseline},
                )
                cls._write_metadata(workspace)
                return workspace

            if resolved_strategy == "protein_run":
                from .protein_run import initialize_run, run_root_for

                run_root = run_root_for(control, workspace_id)
                base_root = run_root.parent.resolve(strict=False)
                if not _inside(run_root, base_root) or run_root == base_root:
                    raise AgentWorkspaceError("unsafe protein run workspace path")
                workspace = AgentWorkspace(
                    workspace_id=workspace_id,
                    task_id=str(task_id or ""),
                    session_id=str(session_id or ""),
                    kind=str(kind or "subagent"),
                    control_workdir=str(control),
                    repo_root="",
                    worktree_root=str(run_root),
                    execution_workdir=str(run_root),
                    strategy="protein_run",
                    domain=str(domain or "protein"),
                    agent_name=str(agent_name or ""),
                    worker_name=str(worker_name or ""),
                    workspace_root=str(run_root),
                    resource_context=dict(resource_context or {}),
                    task_contract=dict(task_contract or {}),
                )
                try:
                    initialize_run(
                        workspace=workspace,
                        resource_context=resource_context,
                    )
                except Exception as exc:
                    raise AgentWorkspaceError(
                        f"failed to create protein run workspace: {exc}"
                    ) from exc
                cls._write_metadata(workspace)
                return workspace

            repo_root = cls._repo_root(control)
            if repo_root is None:
                return cls._fallback(
                    control_workdir=control,
                    task_id=task_id,
                    session_id=session_id,
                    kind=kind,
                    reason="not_a_git_repository",
                    agent_name=agent_name,
                    worker_name=worker_name,
                    domain=domain,
                    resource_context=resource_context,
                    task_contract=task_contract,
                )

            try:
                relative_workdir = control.relative_to(repo_root)
            except ValueError:
                return cls._fallback(
                    control_workdir=control,
                    task_id=task_id,
                    session_id=session_id,
                    kind=kind,
                    reason="control_workdir_outside_repository",
                    agent_name=agent_name,
                    worker_name=worker_name,
                    domain=domain,
                    resource_context=resource_context,
                    task_contract=task_contract,
                )

            workspace_root = cls._workspace_root(repo_root)
            worktree_root = (workspace_root / workspace_id).resolve(strict=False)
            if not _inside(worktree_root, workspace_root) or worktree_root == workspace_root:
                raise ValueError("unsafe agent worktree path")
            workspace_root.mkdir(parents=True, exist_ok=True)

            try:
                base_sha = cls._git_text(repo_root, "rev-parse", "--verify", base_ref)
                cls._run_git(
                    repo_root,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree_root),
                    base_sha,
                )
                if copy_control_changes:
                    cls._copy_control_changes(repo_root, worktree_root)
            except (OSError, subprocess.SubprocessError, AgentWorkspaceError) as exc:
                message = getattr(exc, "stderr", "") or str(exc)
                recovered = cls.load(
                    control_workdir=control,
                    workspace_id=workspace_id,
                )
                if (
                    recovered is not None
                    and recovered.isolated
                    and recovered.status != "cleaned"
                    and recovered.worktree_path.is_dir()
                ):
                    return recovered
                if worktree_root.is_dir():
                    cls._run_git(
                        repo_root,
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree_root),
                        check=False,
                    )
                    cls._run_git(repo_root, "worktree", "prune", check=False)
                raise AgentWorkspaceError(
                    "failed to create isolated agent worktree: "
                    + str(message).strip()[:500]
                ) from exc

            execution = (worktree_root / relative_workdir).resolve(strict=False)
            workspace = AgentWorkspace(
                workspace_id=workspace_id,
                task_id=str(task_id or ""),
                session_id=str(session_id or ""),
                kind=str(kind or "subagent"),
                control_workdir=str(control),
                repo_root=str(repo_root),
                worktree_root=str(worktree_root),
                execution_workdir=str(execution),
                strategy="git_worktree",
                domain=str(domain or ""),
                agent_name=str(agent_name or ""),
                worker_name=str(worker_name or ""),
                workspace_root=str(worktree_root),
                resource_context=dict(resource_context or {}),
                task_contract=dict(task_contract or {}),
                base_ref=str(base_ref or "HEAD"),
                base_sha=base_sha,
            )
            try:
                cls._prepare_git_uploaded_resources(
                    workspace=workspace,
                    resource_context=resource_context,
                )
            except Exception:
                cls._run_git(
                    repo_root,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_root),
                    check=False,
                )
                cls._run_git(repo_root, "worktree", "prune", check=False)
                raise
            cls._write_metadata(workspace)
            return workspace

    @classmethod
    def _copy_control_changes(cls, repo_root: Path, worktree_root: Path) -> None:
        """Copy tracked and untracked source changes into a new worktree."""

        patch = cls._run_git(repo_root, "diff", "--binary", "HEAD").stdout
        if patch:
            cls._run_git(
                worktree_root,
                "apply",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_text=patch,
            )

        untracked = cls._run_git(
            repo_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout
        paths = list(filter(None, untracked.split("\0")))
        include_file = repo_root / ".worktreeinclude"
        if include_file.is_file():
            patterns = [
                line.strip().lstrip("/")
                for line in include_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            ignored = cls._run_git(
                repo_root,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ).stdout
            paths.extend(
                relative
                for relative in filter(None, ignored.split("\0"))
                if any(Path(relative).match(pattern) for pattern in patterns)
            )

        for relative in dict.fromkeys(paths):
            source = (repo_root / relative).resolve(strict=False)
            target = (worktree_root / relative).resolve(strict=False)
            if not _inside(source, repo_root) or not _inside(target, worktree_root):
                raise AgentWorkspaceError("unsafe untracked path while forking workspace")
            if source.is_symlink():
                raise AgentWorkspaceError("session fork refuses symbolic links")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, target)

    @classmethod
    def inspect(cls, workspace: AgentWorkspace) -> AgentWorkspace:
        """Refresh generated/changed paths without modifying the workspace."""

        if not workspace.isolated or workspace.status == "cleaned":
            return workspace
        if workspace.strategy == "snapshot":
            try:
                current = cls._snapshot_files(workspace.worktree_path)
                baseline = dict(workspace.provenance.get("snapshot_baseline") or {})
                workspace.changed_files = sorted(
                    path for path in current.keys() | baseline.keys()
                    if current.get(path) != baseline.get(path)
                )
                workspace.dirty = bool(workspace.changed_files)
                workspace.error = ""
            except Exception as exc:
                workspace.error = str(exc)[:500]
                workspace.dirty = True
            workspace.updated_at = _utc_now()
            cls._write_metadata(workspace)
            return workspace
        if workspace.strategy == "protein_run":
            try:
                from .protein_run import inspect_run, write_manifest

                dirty, changed_files, outputs = inspect_run(workspace)
                workspace.changed_files = changed_files
                workspace.output_artifacts = outputs
                workspace.dirty = dirty
                workspace.error = ""
                workspace.updated_at = _utc_now()
                write_manifest(workspace, outputs=outputs)
            except Exception as exc:
                workspace.error = str(exc)[:500]
            cls._write_metadata(workspace)
            return workspace
        try:
            output = cls._run_git(
                workspace.worktree_path,
                "status",
                "--porcelain=v1",
                "-uall",
            ).stdout.rstrip("\r\n")
            changed_files = []
            for line in output.splitlines():
                path_text = line[3:].strip() if len(line) >= 4 else line.strip()
                if " -> " in path_text:
                    path_text = path_text.split(" -> ", 1)[1]
                if path_text:
                    changed_files.append(path_text)
            input_paths = {
                str(item.get("worktree_relative_path") or "").strip().strip("/")
                for item in list(workspace.input_resources or [])
                if isinstance(item, dict) and bool(item.get("snapshot_created"))
            }
            changed_files = [
                path
                for path in changed_files
                if not any(
                    input_path
                    and (path == input_path or path.startswith(f"{input_path}/"))
                    for input_path in input_paths
                )
            ]
            workspace.changed_files = sorted(dict.fromkeys(changed_files))
            workspace.dirty = bool(workspace.changed_files)
            workspace.error = ""
        except (OSError, subprocess.SubprocessError) as exc:
            workspace.error = str(getattr(exc, "stderr", "") or exc).strip()[:500]
        workspace.updated_at = _utc_now()
        cls._write_metadata(workspace)
        return workspace

    @classmethod
    def finalize(
        cls,
        workspace: AgentWorkspace,
        *,
        outcome: str,
    ) -> AgentWorkspace:
        """Record a terminal outcome and clean only a run with no generated result."""

        with cls._LOCK:
            cls._refresh_persisted_protein_state(workspace)
            if workspace.strategy == "protein_run":
                from .protein_run import (
                    external_jobs_outcome,
                    has_pending_external_jobs,
                    write_checkpoint,
                    write_manifest,
                )

                if has_pending_external_jobs(workspace):
                    workspace.status = "waiting_external"
                    workspace.outcome = ""
                    workspace.finalized_at = ""
                    workspace.retained_reason = "external_job_pending"
                    workspace.updated_at = _utc_now()
                    write_checkpoint(
                        workspace,
                        status="waiting_external",
                        pending_action="Wait for external protein computation to finish.",
                    )
                    write_manifest(
                        workspace,
                        outputs=list(workspace.output_artifacts or []),
                    )
                    cls._write_metadata(workspace)
                    return workspace
                external_outcome = external_jobs_outcome(workspace)
            else:
                external_outcome = ""

            normalized = str(
                external_outcome or outcome or "completed"
            ).strip().lower() or "completed"
            if normalized not in _TERMINAL_OUTCOMES:
                normalized = "failed"
            workspace.outcome = normalized
            workspace.status = normalized if workspace.isolated else "fallback"
            workspace.finalized_at = _utc_now()
            workspace.updated_at = workspace.finalized_at
            cls.inspect(workspace)
            if workspace.strategy == "protein_run":
                try:
                    from .protein_run import write_manifest

                    write_manifest(workspace, outputs=workspace.output_artifacts)
                except Exception:
                    pass
            if not workspace.isolated:
                cls._write_metadata(workspace)
                return workspace
            if workspace.dirty:
                workspace.status = "retained"
                workspace.retained_reason = (
                    f"generated_artifacts_{normalized}"
                    if workspace.strategy == "protein_run"
                    else f"dirty_{normalized}"
                )
                if workspace.strategy == "protein_run":
                    try:
                        from .protein_run import write_checkpoint, write_manifest

                        write_checkpoint(
                            workspace,
                            status="retained",
                            pending_action=(
                                "Review or promote generated protein artifacts."
                                if normalized == "completed"
                                else "Resume from preserved artifacts and tool history."
                            ),
                        )
                        write_manifest(workspace, outputs=workspace.output_artifacts)
                    except Exception:
                        pass
                cls._write_metadata(workspace)
                return workspace
            if _env_bool("TCODE_AGENT_WORKSPACE_CLEANUP_CLEAN_TERMINAL", True):
                return cls.cleanup(workspace, force=False).workspace
            workspace.status = "retained"
            workspace.retained_reason = "cleanup_disabled"
            if workspace.strategy == "protein_run":
                try:
                    from .protein_run import write_checkpoint

                    write_checkpoint(workspace, status="retained")
                except Exception:
                    pass
            cls._write_metadata(workspace)
            return workspace

    @classmethod
    def checkpoint(
        cls,
        workspace: AgentWorkspace,
        *,
        status: str = "paused",
        tool_trace: Optional[list[dict[str, Any]]] = None,
        pending_action: str = "",
    ) -> AgentWorkspace:
        """Persist a resumable non-terminal workspace state."""

        with cls._LOCK:
            cls._refresh_persisted_protein_state(workspace)
            if workspace.strategy == "protein_run":
                from .protein_run import has_pending_external_jobs

                if has_pending_external_jobs(workspace):
                    status = "waiting_external"
                    pending_action = (
                        pending_action
                        or "Wait for external protein computation to finish."
                    )
            cls.inspect(workspace)
            workspace.status = str(status or "paused").strip().lower() or "paused"
            workspace.updated_at = _utc_now()
            if workspace.dirty:
                workspace.retained_reason = (
                    f"generated_artifacts_{workspace.status}"
                    if workspace.strategy == "protein_run"
                    else f"dirty_{workspace.status}"
                )
            if workspace.strategy == "protein_run":
                try:
                    from .protein_run import write_checkpoint, write_manifest

                    write_checkpoint(
                        workspace,
                        status=workspace.status,
                        tool_trace=tool_trace,
                        pending_action=pending_action,
                    )
                    write_manifest(workspace, outputs=workspace.output_artifacts)
                except Exception:
                    pass
            cls._write_metadata(workspace)
            return workspace

    @classmethod
    def cleanup(
        cls,
        workspace: AgentWorkspace,
        *,
        force: bool = False,
    ) -> WorkspaceCleanupResult:
        """Remove one validated workspace; preserve generated/changed results by default."""

        with cls._LOCK:
            cls.inspect(workspace)
            if not workspace.isolated:
                return WorkspaceCleanupResult(False, True, "not_isolated", workspace)
            if workspace.status == "cleaned" or not workspace.worktree_path.exists():
                workspace.status = "cleaned"
                workspace.cleaned_at = workspace.cleaned_at or _utc_now()
                cls._write_metadata(workspace)
                return WorkspaceCleanupResult(True, False, "already_absent", workspace)
            if workspace.dirty and not force:
                workspace.status = "retained"
                reason = (
                    "generated_artifacts"
                    if workspace.strategy == "protein_run"
                    else "dirty_worktree"
                )
                workspace.retained_reason = reason
                cls._write_metadata(workspace)
                return WorkspaceCleanupResult(False, True, reason, workspace)

            if workspace.strategy == "snapshot":
                root = cls._snapshot_root(Path(workspace.control_workdir)).resolve(strict=False)
                if not _inside(workspace.worktree_path, root) or workspace.worktree_path == root:
                    raise ValueError("refusing to remove an unsafe agent snapshot path")
                shutil.rmtree(workspace.worktree_path)
                workspace.status = "cleaned"
                workspace.cleaned_at = _utc_now()
                workspace.updated_at = workspace.cleaned_at
                workspace.retained_reason = ""
                cls._write_metadata(workspace)
                return WorkspaceCleanupResult(True, False, "cleaned", workspace)

            if workspace.strategy == "protein_run":
                from .protein_run import make_tree_writable

                configured = str(
                    os.environ.get("TCODE_PROTEIN_RUNS_ROOT", "") or ""
                ).strip()
                base = (
                    Path(configured).expanduser()
                    if configured
                    else Path(workspace.control_workdir)
                    / ".tcode"
                    / "runs"
                    / "protein"
                )
                if not base.is_absolute():
                    base = Path(workspace.control_workdir) / base
                root = base.resolve(strict=False)
                if not _inside(workspace.worktree_path, root) or workspace.worktree_path == root:
                    raise ValueError("refusing to remove an unsafe protein run path")
                try:
                    make_tree_writable(workspace.worktree_path)
                    shutil.rmtree(workspace.worktree_path)
                except OSError as exc:
                    workspace.status = "retained"
                    workspace.retained_reason = "cleanup_failed"
                    workspace.error = str(exc)[:500]
                    workspace.updated_at = _utc_now()
                    cls._write_metadata(workspace)
                    return WorkspaceCleanupResult(
                        False,
                        True,
                        "cleanup_failed",
                        workspace,
                    )
                workspace.status = "cleaned"
                workspace.cleaned_at = _utc_now()
                workspace.updated_at = workspace.cleaned_at
                workspace.retained_reason = ""
                cls._write_metadata(workspace)
                return WorkspaceCleanupResult(True, False, "cleaned", workspace)

            root = cls._workspace_root(Path(workspace.repo_root))
            if not _inside(workspace.worktree_path, root) or workspace.worktree_path == root:
                raise ValueError("refusing to remove an unsafe agent worktree path")
            try:
                cls._remove_git_uploaded_resources(workspace)
                args = ["worktree", "remove"]
                if force:
                    args.append("--force")
                args.append(str(workspace.worktree_path))
                cls._run_git(Path(workspace.repo_root), *args)
                cls._run_git(Path(workspace.repo_root), "worktree", "prune", check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                workspace.status = "retained"
                workspace.retained_reason = "cleanup_failed"
                workspace.error = str(getattr(exc, "stderr", "") or exc).strip()[:500]
                workspace.updated_at = _utc_now()
                cls._write_metadata(workspace)
                return WorkspaceCleanupResult(False, True, "cleanup_failed", workspace)

            workspace.status = "cleaned"
            workspace.cleaned_at = _utc_now()
            workspace.updated_at = workspace.cleaned_at
            workspace.retained_reason = ""
            cls._write_metadata(workspace)
            return WorkspaceCleanupResult(True, False, "cleaned", workspace)

    @classmethod
    def execution_prompt_hint(cls, workspace: AgentWorkspace) -> str:
        if workspace.strategy != "protein_run":
            return ""
        from .protein_run import execution_prompt_hint

        return execution_prompt_hint(workspace)

    @classmethod
    def record_provenance(
        cls,
        workspace: AgentWorkspace,
        *,
        agent: Any = None,
        tool_trace: Optional[list[dict[str, Any]]] = None,
        attempt: int = 0,
        outcome: str = "",
    ) -> AgentWorkspace:
        if workspace.strategy != "protein_run":
            return workspace
        cls._refresh_persisted_protein_state(workspace)
        from .protein_run import record_provenance

        record_provenance(
            workspace,
            agent=agent,
            tool_trace=tool_trace,
            attempt=attempt,
            outcome=outcome,
        )
        cls._write_metadata(workspace)
        return workspace

    @classmethod
    def record_external_job(
        cls,
        workspace: AgentWorkspace,
        *,
        identifier: str,
        target_server: str,
        target_tool: str,
        status: str,
        phase: str,
        attempt_id: str = "",
        job_id: str = "",
        supersedes_attempt_id: str = "",
        arguments: Optional[dict[str, Any]] = None,
        result_summary: str = "",
        error_summary: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> AgentWorkspace:
        if workspace.strategy != "protein_run":
            return workspace
        from .protein_run import record_external_job

        with cls._LOCK:
            cls._refresh_persisted_protein_state(workspace)
            record_external_job(
                workspace,
                identifier=identifier,
                target_server=target_server,
                target_tool=target_tool,
                status=status,
                phase=phase,
                attempt_id=attempt_id,
                job_id=job_id,
                supersedes_attempt_id=supersedes_attempt_id,
                arguments=arguments,
                result_summary=result_summary,
                error_summary=error_summary,
                metadata=metadata,
            )
            cls._write_metadata(workspace)
        return workspace

    @classmethod
    def begin_run_action(
        cls,
        workspace: AgentWorkspace,
        *,
        action: str,
        attempt_id: str,
        idempotency_key: str = "",
        reason: str = "",
    ) -> tuple[dict[str, Any], bool]:
        if workspace.strategy != "protein_run":
            raise AgentWorkspaceError("run actions require a protein_run workspace")
        from .protein_run import begin_run_action

        with cls._LOCK:
            cls._refresh_persisted_protein_state(workspace)
            record, created = begin_run_action(
                workspace,
                action=action,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                reason=reason,
            )
            cls._write_metadata(workspace)
        return record, created

    @classmethod
    def finish_run_action(
        cls,
        workspace: AgentWorkspace,
        *,
        action_id: str,
        status: str,
        result: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> dict[str, Any]:
        if workspace.strategy != "protein_run":
            raise AgentWorkspaceError("run actions require a protein_run workspace")
        from .protein_run import finish_run_action

        with cls._LOCK:
            cls._refresh_persisted_protein_state(workspace)
            record = finish_run_action(
                workspace,
                action_id=action_id,
                status=status,
                result=result,
                error=error,
            )
            cls._write_metadata(workspace)
        return record

    @classmethod
    def record_review_provenance(
        cls,
        workspace: AgentWorkspace,
        *,
        review: dict[str, Any],
        review_attempt: int,
    ) -> AgentWorkspace:
        if workspace.strategy != "protein_run":
            return workspace
        from .protein_run import record_review_provenance

        record_review_provenance(
            workspace,
            review=dict(review or {}),
            review_attempt=review_attempt,
        )
        cls._write_metadata(workspace)
        return workspace
