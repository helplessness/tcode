"""Prepare and freeze isolated execution directories for research experiments."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class ExperimentExecutionManager:
    MODES = {"shared_workspace", "isolated_directory", "git_worktree"}

    @staticmethod
    def _run(args: list[str], *, cwd: Path) -> str:
        return subprocess.run(
            args, cwd=cwd, check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()

    @classmethod
    def prepare(cls, *, research_id: str, experiment_id: str, mode: str, workspace: str | None, base_commit: str = "") -> dict[str, Any]:
        root = Path(workspace or os.getcwd()).expanduser().resolve()
        if mode == "shared_workspace":
            return {"mode": mode, "workdir": str(root), "base_commit": cls._head(root), "state": "active"}
        safe_research = re.sub(r"[^A-Za-z0-9_.-]", "-", research_id)
        safe_experiment = re.sub(r"[^A-Za-z0-9_.-]", "-", experiment_id)
        target = root / ".tcode" / "research" / "runs" / safe_research / safe_experiment
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "isolated_directory":
            target.mkdir(exist_ok=False)
            return {"mode": mode, "workdir": str(target), "base_commit": "", "state": "active"}
        if not (root / ".git").exists() and not cls._is_git(root):
            raise ValueError("git_worktree execution requires a Git workspace")
        if cls._run(["git", "status", "--porcelain"], cwd=root):
            raise ValueError("git_worktree execution requires a clean Workspace; commit or stash changes first")
        base = base_commit or cls._head(root)
        if not base:
            raise ValueError("git_worktree execution requires a committed base revision")
        branch = f"tcode/research/{safe_research}/{safe_experiment}"
        cls._run(["git", "worktree", "add", "-b", branch, str(target), base], cwd=root)
        return {"mode": mode, "workdir": str(target), "base_commit": base, "branch": branch, "state": "active"}

    @classmethod
    def freeze(cls, manifest: dict[str, Any]) -> dict[str, Any]:
        mode = str(manifest.get("mode") or "shared_workspace")
        path = Path(str(manifest.get("workdir") or "")).expanduser()
        if mode != "git_worktree" or not path.exists():
            return {"state": "frozen", "result_commit": str(manifest.get("base_commit") or "")}
        status = cls._run(["git", "status", "--porcelain"], cwd=path)
        if status:
            cls._validate_new_files(path)
            cls._run(["git", "add", "-A"], cwd=path)
            cls._run(["git", "commit", "-m", f"research: freeze {path.name}"], cwd=path)
        result = cls._head(path)
        diff = subprocess.run(
            ["git", "diff", "--binary", str(manifest.get("base_commit") or result), result],
            cwd=path, check=True, capture_output=True, timeout=30,
        ).stdout
        return {
            "state": "frozen", "result_commit": result,
            "code_diff_digest": hashlib.sha256(diff).hexdigest(),
        }

    @classmethod
    def cleanup(cls, manifest: dict[str, Any], *, workspace: str | None) -> None:
        path = Path(str(manifest.get("workdir") or "")).expanduser()
        root = Path(workspace or os.getcwd()).expanduser().resolve()
        managed_root = (root / ".tcode" / "research" / "runs").resolve()
        try:
            path.resolve().relative_to(managed_root)
        except (ValueError, OSError):
            return
        if not path.exists():
            return
        if str(manifest.get("mode") or "") == "git_worktree":
            cls._run(["git", "worktree", "remove", "--force", str(path)], cwd=root)
            branch = str(manifest.get("branch") or "").strip()
            if branch:
                subprocess.run(["git", "branch", "-D", branch], cwd=root, capture_output=True, timeout=30)
        else:
            import shutil
            shutil.rmtree(path)

    @classmethod
    def _validate_new_files(cls, path: Path) -> None:
        names = cls._run(["git", "ls-files", "--others", "--exclude-standard"], cwd=path).splitlines()
        forbidden = re.compile(r"(^|/)(\.env($|\.)|.*\.(pem|key|p12|pfx)$)", re.I)
        for name in names:
            candidate = (path / name).resolve()
            if forbidden.search(name) or candidate.stat().st_size > 50 * 1024 * 1024:
                raise ValueError(f"experiment contains an unsafe file that cannot be frozen: {name}")

    @classmethod
    def _head(cls, path: Path) -> str:
        try:
            return cls._run(["git", "rev-parse", "HEAD"], cwd=path)
        except (OSError, subprocess.SubprocessError):
            return ""

    @classmethod
    def _is_git(cls, path: Path) -> bool:
        try:
            return cls._run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path) == "true"
        except (OSError, subprocess.SubprocessError):
            return False
