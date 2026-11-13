"""Per-invocation, approved snapshots over the existing workspace artifact path.

No registry or job store: outputs are normal files and the tool result supplies
artifact_paths to the existing research event/transport pipeline.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import stat
import uuid
from pathlib import Path

from ..tools.path_policy import resolve_workspace_io_path
from .permissions import PluginPermissionError

# Bounded below the existing 512 KiB worker frame, without changing transport.
MAX_TEXT_BYTES = 192 * 1024
MAX_ANALYSIS_BYTES = 4 * 1024 * 1024
TEXT_PAGE_CHARS = 48 * 1024


def _publish_new_file(root: Path, relative: str, data: bytes):
    """Only this new artifact path: atomic create, never open the old target."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise ValueError("invalid artifact destination")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".plugin-artifact-" + uuid.uuid4().hex
    created = False
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        created = True
        with os.fdopen(out, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic even when a competing writer creates the name after approval.
        os.link(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
        os.close(fd)


class PluginArtifacts:
    def __init__(self, context, workspace, effect):
        self.context, self.workspace, self.effect = context, workspace, effect
        self.active = True
        self._text_page = None

    def close(self):
        self.active = False
        self._text_page = None

    def _require(self, effect):
        if self.context.abort is not None and self.context.abort.is_set():
            raise asyncio.CancelledError("artifact invocation was cancelled")
        if not self.active or self.workspace is None:
            raise PluginPermissionError("artifact capability is not active")
        if {"none": 0, "read": 1, "write": 2, "process": 3}.get(self.effect, 0) < effect:
            raise PluginPermissionError("artifact capability exceeds declared side effect")

    def _owner(self):
        extra = self.context.extra or {}
        owner = {k: str(extra.get(k) or "") for k in ("tenant_id", "user_id", "workspace_id")}
        owner["session_id"] = str(self.context.session_id or "")
        if not all(owner.values()):
            raise PluginPermissionError("artifact owner identity is unavailable")
        return owner

    async def _path(self, path, operation):
        self._require(1 if operation == "read" else 2)
        workdir = str((self.context.extra or {}).get("workdir") or "").strip()
        if not workdir:
            raise PluginPermissionError("artifact workspace identity is unavailable")
        decision = resolve_workspace_io_path(path, self.context, tool_id=operation, operation=operation)
        root = Path(workdir).resolve()
        target = Path(decision.host_path)
        if decision.blocked or not target.is_relative_to(root) or target == root:
            raise PluginPermissionError("artifact path is outside the active workspace")
        resolver = self.workspace.resolve_read_path if operation == "read" else self.workspace.resolve_write_path
        if Path(resolver(str(target))) != target:
            raise PluginPermissionError("artifact path changed during validation")
        if not callable((self.context.extra or {}).get("__permission_ask__")):
            raise PluginPermissionError("artifact authorization is unavailable")
        if not await self.context.ask(operation, [str(target), decision.container_path]):
            raise PluginPermissionError("artifact access denied; no file was read or written")
        self._require(1 if operation == "read" else 2)
        return root, target.relative_to(root).as_posix()

    async def read_text(self, path, *, sha256="", snapshot=False, max_bytes=MAX_TEXT_BYTES):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ANALYSIS_BYTES:
            raise ValueError("artifact read budget must be 1 to 4194304 bytes")
        root, relative = await self._path(path, "read")
        # Walk from the already-authorized workspace without resolving symlinks
        # again after approval. Reject special files and hardlinks as well.
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = Path(relative).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("artifact must be a regular text file without hardlinks")
                if info.st_size > max_bytes:
                    raise ValueError(f"artifact exceeds {max_bytes // 1024} KiB analysis budget; no partial analysis")
                data = stream.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"artifact exceeds {max_bytes // 1024} KiB analysis budget; no partial analysis")
        finally:
            os.close(fd)
        digest = hashlib.sha256(data).hexdigest()
        if sha256 and digest != sha256:
            raise ValueError("artifact fingerprint changed; recheck the source")
        text = data.decode("utf-8-sig")
        if snapshot:
            saved = json.loads(text)
            if not isinstance(saved, dict) or saved.get("format") != "tcode.plugin-snapshot.v1" or saved.get("owner") != self._owner():
                raise PluginPermissionError("snapshot belongs to another session or workspace")
        result = {"path": "/workspace/" + relative, "sha256": digest, "text": text}
        if max_bytes <= MAX_TEXT_BYTES and len(json.dumps(result, ensure_ascii=False).encode()) > 448 * 1024:
            raise ValueError("artifact text exceeds the worker transport budget")
        return result

    async def read_text_page(self, path="", *, token="", **kwargs):
        """Worker-only paging of one approved immutable read, never model context.

        One buffer per invocation, cleared on completion/close. Continuations do
        not reopen a file or extend the original authorization.
        """
        self._require(1)
        if not token and kwargs.get("max_bytes", MAX_TEXT_BYTES) == MAX_TEXT_BYTES:
            return await self.read_text(path, **kwargs)
        if token:
            if self._text_page is None or token != self._text_page[0]:
                raise PluginPermissionError("artifact read continuation is no longer active")
            _, result, offset = self._text_page
        else:
            self._text_page = None
            result = await self.read_text(path, **kwargs)
            token, offset = uuid.uuid4().hex, 0
        text = result["text"]
        end = min(offset + TEXT_PAGE_CHARS, len(text))
        more = end < len(text)
        self._text_page = (token, result, end) if more else None
        return {**result, "text": text[offset:end], "next_token": token if more else ""}

    async def publish_json(self, value):
        self._require(2)
        owner = self._owner()
        # Read only authoritative session metadata; no model-supplied goal IDs.
        from ..session.manager import SessionManager
        session = await SessionManager.get(owner["session_id"], **{k: owner[k] for k in ("tenant_id", "user_id", "workspace_id")})
        if session is None:
            raise PluginPermissionError("artifact session is unavailable")
        metadata = (session.metadata or {}) if session else {}
        goal = metadata.get("goal_mode")
        goal = goal if isinstance(goal, dict) else {}
        extra = self.context.extra or {}
        request_id = str(extra.get("request_id") or "").strip()
        in_goal = (callable(extra.get("__goal_checkpoint__")) and bool(request_id)
                   and isinstance(goal.get("run_ids"), list) and request_id in goal["run_ids"]
                   and goal.get("status") in {"ready", "running", "waiting_external", "recovering"})
        linkage = {"research_id": str(metadata.get("active_research_id") or ""),
                   "goal_id": str(goal.get("id") or "") if in_goal else "",
                   "message_id": str(self.context.message_id or ""), "call_id": str(self.context.call_id or "")}
        document = {"format": "tcode.plugin-snapshot.v1", "owner": owner, "linkage": linkage, "data": value}
        data = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        if len(data) > MAX_TEXT_BYTES:
            raise ValueError("snapshot exceeds 192 KiB; split the input analysis")
        relative = "outputs/plugin_analysis/" + uuid.uuid4().hex + ".json"
        root, relative = await self._path("/workspace/" + relative, "write")
        _publish_new_file(root, relative, data)
        return {"path": "/workspace/" + relative, "sha256": hashlib.sha256(data).hexdigest(),
                "artifact_path": str(root / relative), "linkage": linkage}
