from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from .base import SandboxSpec, WorkspacePathMap
from .pathmap import assert_inside_workspace

from ...file_lock import lock_file, unlock_file


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.RLock()
_SNAPSHOT_COPY_ATTEMPTS = 3


@contextmanager
def _parent_fd(root_fd: int, relative: str, *, create: bool = False):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(part in {".", ".."} for part in parts):
        raise ValueError("unsafe snapshot path")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _remove_at(parent: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("safe directory deletion is unavailable")
        shutil.rmtree(name, dir_fd=parent)
    else:
        os.unlink(name, dir_fd=parent)


def _writeback(source: Path, workspace: Path, deleted: list[str], directories: list[str], files: list[str], manifest: dict) -> None:
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    task_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for relative in sorted(deleted, key=lambda item: item.count("/"), reverse=True):
            with _parent_fd(source_fd, relative) as (parent, name):
                _remove_at(parent, name)
        for relative in sorted(directories, key=lambda item: item.count("/")):
            with _parent_fd(source_fd, relative, create=True) as (parent, name):
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        _remove_at(parent, name)
                except FileNotFoundError:
                    pass
                try:
                    os.mkdir(name, dir_fd=parent)
                except FileExistsError:
                    pass
        for relative in files:
            with _parent_fd(source_fd, relative, create=True) as (parent, name), _parent_fd(task_fd, relative) as (task_parent, task_name):
                temp = f".tcode-writeback-{uuid.uuid4().hex}"
                try:
                    entry = manifest[relative]
                    if entry["type"] == "symlink":
                        target = os.readlink(task_name, dir_fd=task_parent)
                        if target != entry["target"]:
                            raise RuntimeError("snapshot file changed during writeback")
                        os.symlink(target, temp, dir_fd=parent)
                    else:
                        input_fd = os.open(task_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=task_parent)
                        try:
                            if not stat.S_ISREG(os.fstat(input_fd).st_mode):
                                raise ValueError("snapshot input is not a regular file")
                            output_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                            digest = hashlib.sha256()
                            with os.fdopen(output_fd, "wb") as output, os.fdopen(input_fd, "rb", closefd=False) as input_stream:
                                for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                                    digest.update(chunk)
                                    output.write(chunk)
                                os.fchmod(output.fileno(), int(entry["mode"]))
                            if digest.hexdigest() != entry["sha256"]:
                                raise RuntimeError("snapshot file changed during writeback")
                        finally:
                            os.close(input_fd)
                    try:
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            _remove_at(parent, name)
                    except FileNotFoundError:
                        pass
                    os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                finally:
                    try:
                        os.unlink(temp, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        for relative in sorted(directories, key=lambda item: item.count("/"), reverse=True):
            with _parent_fd(source_fd, relative) as (parent, name):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fchmod(child, int(manifest[relative].get("mode", 0o755)))
                finally:
                    os.close(child)
    finally:
        os.close(task_fd)
        os.close(source_fd)


def _is_rebuildable_sqlite_sidecar(path: Path) -> bool:
    name = path.name.lower()
    if not name.endswith("-shm"):
        return False
    database_name = name[:-4]
    return Path(database_name).suffix in {".db", ".sqlite", ".sqlite3"}


def build_manifest(root: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}

    def visit(fd: int, relative: Path) -> None:
        for name in sorted(os.listdir(fd)):
            rel = relative / name
            if _is_rebuildable_sqlite_sidecar(rel):
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        manifest[rel.as_posix()] = {"type": "dir", "mode": mode}
                        visit(child, rel)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    try:
                        actual = os.fstat(child)
                        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                            raise RuntimeError("workspace file changed during manifest creation")
                        digest = hashlib.sha256()
                        with os.fdopen(child, "rb", closefd=False) as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                digest.update(chunk)
                        manifest[rel.as_posix()] = {"type": "file", "mode": mode, "size": actual.st_size, "sha256": digest.hexdigest()}
                    finally:
                        os.close(child)
                elif stat.S_ISLNK(info.st_mode):
                    manifest[rel.as_posix()] = {"type": "symlink", "target": os.readlink(name, dir_fd=fd)}
                else:
                    raise ValueError(f"workspace contains unsupported special file: {rel}")
            except FileNotFoundError:
                continue

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(fd, Path())
    finally:
        os.close(fd)
    return manifest


def _remove_path(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_workspace(source: Path, target: Path) -> dict[str, dict[str, Any]]:
    for _attempt in range(_SNAPSHOT_COPY_ATTEMPTS):
        _remove_path(target)
        baseline = build_manifest(source)
        target.mkdir()
        directories = [path for path, entry in baseline.items() if entry["type"] == "dir"]
        files = [path for path, entry in baseline.items() if entry["type"] in {"file", "symlink"}]
        try:
            _writeback(target, source, [], directories, files, baseline)
            snapshot_manifest = build_manifest(target)
            if snapshot_manifest == baseline == build_manifest(source):
                return snapshot_manifest
        except FileNotFoundError:
            continue
    raise RuntimeError("workspace changed while the sandbox snapshot was being created")


class WorkspaceSnapshotManager:
    def __init__(self, snapshot_root: str, conflict_root: str):
        self.snapshot_root = Path(snapshot_root).expanduser().resolve(strict=False)
        self.conflict_root = Path(conflict_root).expanduser().resolve(strict=False)
        self.lock_root = self.snapshot_root / ".locks"
        for path in (self.snapshot_root, self.conflict_root, self.lock_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _lock_name(self, workspace: Path) -> str:
        return hashlib.sha256(workspace.as_posix().encode("utf-8")).hexdigest()

    @contextmanager
    def _workspace_lock(self, workspace: Path) -> Iterator[None]:
        lock_name = self._lock_name(workspace)
        with _LOCAL_LOCKS_GUARD:
            local_lock = _LOCAL_LOCKS.setdefault(lock_name, threading.RLock())
        with local_lock:
            lock_path = self.lock_root / f"{lock_name}.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                lock_file(handle)
                try:
                    yield
                finally:
                    unlock_file(handle)

    def create(
        self,
        spec: SandboxSpec,
        *,
        snapshot_id: str = "",
    ) -> tuple[SandboxSpec, dict[str, Any]]:
        source = spec.pathmap.host_workspace.expanduser().resolve(strict=True)
        relative_cwd = spec.host_cwd.expanduser().resolve(strict=True).relative_to(source)
        identifier = snapshot_id or f"snap_{uuid.uuid4().hex[:20]}"
        record_dir = self.snapshot_root / identifier
        snapshot_workspace = record_dir / "workspace"
        if record_dir.exists():
            raise RuntimeError(f"sandbox snapshot already exists: {identifier}")
        record_dir.mkdir(parents=True, mode=0o700)

        try:
            with self._workspace_lock(source):
                baseline = _copy_workspace(source, snapshot_workspace)
            manifest_path = record_dir / "baseline.json"
            manifest_path.write_text(
                json.dumps(baseline, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(record_dir, ignore_errors=True)
            raise

        snapshot = {
            "snapshot_id": identifier,
            "source_workspace": source.as_posix(),
            "snapshot_workspace": snapshot_workspace.as_posix(),
            "baseline_manifest": manifest_path.as_posix(),
            "created_at": time.time(),
        }
        prepared = replace(
            spec,
            pathmap=WorkspacePathMap(
                workspace_id=spec.pathmap.workspace_id,
                host_workspace=snapshot_workspace,
                container_workspace=spec.pathmap.container_workspace,
            ),
            host_cwd=snapshot_workspace / relative_cwd,
            metadata={
                **dict(spec.metadata or {}),
                "workspace_snapshot": snapshot,
            },
        )
        return prepared, snapshot

    def _load_snapshot(
        self,
        snapshot: dict[str, Any],
    ) -> tuple[Path, Path, Path, dict[str, dict[str, Any]]]:
        identifier = str(snapshot.get("snapshot_id") or "").strip()
        if not identifier:
            raise ValueError("workspace snapshot id is missing")
        record_dir = assert_inside_workspace(
            self.snapshot_root / identifier,
            self.snapshot_root,
        )
        source = Path(str(snapshot.get("source_workspace") or "")).resolve(strict=True)
        workspace = assert_inside_workspace(
            Path(str(snapshot.get("snapshot_workspace") or "")),
            record_dir,
        )
        manifest_path = assert_inside_workspace(
            Path(str(snapshot.get("baseline_manifest") or "")),
            record_dir,
        )
        baseline = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(baseline, dict):
            raise ValueError("workspace snapshot baseline is invalid")
        return record_dir, source, workspace, baseline

    def finalize(
        self,
        snapshot: dict[str, Any],
        *,
        commit: bool,
    ) -> dict[str, Any]:
        record_dir, source, workspace, baseline = self._load_snapshot(snapshot)
        if not commit:
            shutil.rmtree(record_dir, ignore_errors=True)
            return {"status": "discarded", "changed_paths": [], "conflicts": []}

        task_manifest = build_manifest(workspace)
        changed_paths = sorted(
            path
            for path in set(baseline) | set(task_manifest)
            if baseline.get(path) != task_manifest.get(path)
        )
        with self._workspace_lock(source):
            current_manifest = build_manifest(source)
            conflicts = sorted(
                path
                for path in changed_paths
                if current_manifest.get(path) != baseline.get(path)
                and current_manifest.get(path) != task_manifest.get(path)
            )
            destructive_directories = [
                path
                for path in changed_paths
                if (baseline.get(path) or {}).get("type") == "dir"
                and (task_manifest.get(path) or {}).get("type") != "dir"
            ]
            for directory in destructive_directories:
                prefix = f"{directory}/"
                for current_path, current_entry in current_manifest.items():
                    if not current_path.startswith(prefix):
                        continue
                    if current_entry != baseline.get(current_path):
                        conflicts.append(current_path)
            conflicts = sorted(set(conflicts))
            if conflicts:
                archive = self.conflict_root / str(snapshot["snapshot_id"])
                if archive.exists():
                    shutil.rmtree(archive)
                shutil.move(record_dir.as_posix(), archive.as_posix())
                return {
                    "status": "conflict",
                    "changed_paths": changed_paths,
                    "conflicts": conflicts,
                    "conflict_archive": archive.as_posix(),
                }

            deleted = [
                path
                for path in changed_paths
                if path in baseline and path not in task_manifest
            ]

            directories = [
                path
                for path in changed_paths
                if (task_manifest.get(path) or {}).get("type") == "dir"
            ]

            files = [
                path
                for path in changed_paths
                if (task_manifest.get(path) or {}).get("type") in {"file", "symlink"}
            ]
            _writeback(source, workspace, deleted, directories, files, task_manifest)

        shutil.rmtree(record_dir, ignore_errors=True)
        return {
            "status": "committed",
            "changed_paths": changed_paths,
            "conflicts": [],
        }

    def discard(self, snapshot: dict[str, Any]) -> None:
        try:
            record_dir, _source, _workspace, _baseline = self._load_snapshot(snapshot)
        except Exception:
            return
        shutil.rmtree(record_dir, ignore_errors=True)
