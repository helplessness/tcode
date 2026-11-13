"""Read-only asset history. Observed changes are never attributed to an actor."""
from __future__ import annotations

import asyncio
import difflib
import functools
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_FILE = 8 * 1024 * 1024
MAX_STORE = 512 * 1024 * 1024


@contextmanager
def connect(root: Path):
    directory = root / ".tcode-asset-history"
    if directory.is_symlink():
        raise ValueError("History directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    database = directory / "history.sqlite3"
    if database.is_symlink():
        raise ValueError("History database must not be a symlink")
    db = sqlite3.connect(database, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, content BLOB)")
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, session TEXT, started REAL, ended REAL, before_json TEXT, after_json TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS runs_started_idx ON runs(started DESC)")
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()


def capture(root: Path, db) -> dict:
    from ..server.workspace_files import collect_user_visible_workspace_files
    # Reuse the public asset allowlist; never archive runtime configuration.
    from ..server.state import ServerState
    files, _, truncated = collect_user_visible_workspace_files(root, server_workdir=ServerState.get_workdir())
    entries = {}
    used = db.execute("SELECT COALESCE(SUM(length(content)), 0) FROM blobs").fetchone()[0]
    for item in files:
        try:
            path = item.path
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                continue
            info = path.stat()
            entry = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "hash": None}
            if info.st_size <= MAX_FILE:
                with path.open("rb") as stream:
                    data = stream.read(MAX_FILE + 1)
                after = path.stat()
                if len(data) <= MAX_FILE and (info.st_size, info.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                    digest = hashlib.sha256(data).hexdigest()
                    entry["hash"] = digest
                    exists = db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()
                    if exists or used + len(data) <= MAX_STORE:
                        db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (digest, data))
                        if not exists:
                            used += len(data)
                        entry["saved"] = True
            entries[item.relative_path] = entry
        except OSError:
            truncated = True
    return {"files": entries, "incomplete": truncated}


def _changes(before: dict, after: dict) -> list[dict]:
    changes = []
    before_files, after_files = before["files"], after["files"]
    for name in sorted(before_files.keys() | after_files.keys()):
        old, new = before_files.get(name), after_files.get(name)
        if old == new or (old and new and old.get("hash") and old["hash"] == new.get("hash")):
            continue
        changes.append({"path": name, "status": "added" if old is None else "deleted" if new is None else "modified", "before": old, "after": new})
    return changes


def begin(root: Path, session: str, identifier: str = "") -> str:
    identifier = identifier or uuid.uuid4().hex
    with connect(root) as db:
        if db.execute("SELECT 1 FROM runs WHERE id=?", (identifier,)).fetchone():
            return identifier
        baseline = capture(root, db)
        db.execute("INSERT INTO runs VALUES (?, ?, ?, NULL, ?, NULL)", (identifier, session, time.time(), json.dumps(baseline)))
    return identifier


def finish(root: Path, identifier: str):
    if not (root / ".tcode-asset-history" / "history.sqlite3").is_file():
        return
    with connect(root) as db:
        row = db.execute("SELECT ended, before_json FROM runs WHERE id=?", (identifier,)).fetchone()
        if row is None or row[0] is not None:
            return
        final = capture(root, db)
        if not _changes(json.loads(row[1]), final):
            db.execute("DELETE FROM runs WHERE id=?", (identifier,))
        else:
            db.execute("UPDATE runs SET ended=?, after_json=? WHERE id=?", (time.time(), json.dumps(final), identifier))


async def record_async_job(workdir: str, identifier: str, *, session: str = "", completed: bool = False):
    """Best effort, durable job key; recovery must never invent a baseline."""
    if not workdir or not identifier or not Path(workdir).is_dir():
        return
    try:
        if completed:
            await asyncio.to_thread(finish, Path(workdir), f"job:{identifier}")
        else:
            await asyncio.to_thread(begin, Path(workdir), session, f"job:{identifier}")
    except Exception:
        logging.getLogger(__name__).exception("Async asset history recording failed")


def list_runs(
    root: Path,
    query: str = "",
    offset: int = 0,
    limit: int = 50,
    started_from: float | None = None,
    started_before: float | None = None,
) -> dict:
    """Return a bounded, searchable page without loading snapshot JSON."""
    database = root / ".tcode-asset-history" / "history.sqlite3"
    if not database.exists():
        return {"runs": [], "total": 0, "offset": 0, "limit": limit}
    offset, limit = max(0, offset), min(max(1, limit), 100)
    with connect(root) as db:
        clauses, args = [], []
        if query.strip():
            clauses.append("(id LIKE ? OR session LIKE ?)")
            term = f"%{query.strip()}%"
            args.extend((term, term))
        if started_from is not None:
            clauses.append("started >= ?")
            args.append(started_from)
        if started_before is not None:
            clauses.append("started < ?")
            args.append(started_before)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        total = db.execute(f"SELECT COUNT(*) FROM runs {where}", args).fetchone()[0]
        rows = db.execute(
            f"SELECT id, session, started, ended FROM runs {where} ORDER BY started DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return {
        "runs": [
            {"id": item_id, "session": session, "started": started, "ended": ended}
            for item_id, session, started, ended in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def history(
    root: Path,
    identifier: str = "",
    path: str = "",
    *,
    change_query: str = "",
    change_status: str = "",
    change_offset: int = 0,
    change_limit: int = 100,
) -> dict:
    if not (root / ".tcode-asset-history" / "history.sqlite3").exists():
        return {"runs": [], "changes": [], "change_total": 0, "notice": "尚无历史版本；新任务开始后自动记录。"}
    with connect(root) as db:
        if not identifier:
            latest = db.execute("SELECT id FROM runs ORDER BY started DESC LIMIT 1").fetchone()
            if latest is None:
                return {"runs": [], "changes": [], "change_total": 0}
            identifier = latest[0]
        row = db.execute("SELECT session, started, ended, before_json, after_json FROM runs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("记录不存在")
        session, started, ended, before_json, after_json = row
        before = json.loads(before_json)
        after = json.loads(after_json) if after_json else capture(root, db)
        all_changes = _changes(before, after)
        selected = next((item for item in all_changes if item["path"] == path), None) if path else None
        if path and selected is None:
            raise ValueError("该文件不在所选变更记录中")
        filtered = all_changes
        if change_query.strip():
            needle = change_query.strip().casefold()
            filtered = [item for item in filtered if needle in item["path"].casefold()]
        if change_status in {"added", "deleted", "modified"}:
            filtered = [item for item in filtered if item["status"] == change_status]
        change_offset = max(0, change_offset)
        change_limit = min(max(1, change_limit), 200)
        page = filtered[change_offset:change_offset + change_limit]
        result = {
            "runs": [{"id": identifier, "session": session, "started": started, "ended": ended}],
            "run_id": identifier,
            "selected_run": {"id": identifier, "session": session, "started": started, "ended": ended},
            "changes": page,
            "change_total": len(filtered),
            "change_offset": change_offset,
            "change_limit": change_limit,
            "incomplete": before["incomplete"] or after["incomplete"],
            "live": after_json is None,
            "notice": "任务期间观测到的变化，来源未确认；可能包含手动或并发任务修改。",
        }
        if path:
            result["selected_change"] = selected
            texts = []
            for snapshot in (before, after):
                entry = snapshot["files"].get(path)
                blob = db.execute("SELECT content FROM blobs WHERE hash=?", (entry.get("hash"),)).fetchone() if entry else (b"",)
                if blob is None:
                    result["unavailable"] = "未保存完整内容（单文件超过 8 MiB、存储达到 512 MiB 或读取时发生变化），仅展示元数据。"
                    return result
                try:
                    if b"\0" in blob[0]:
                        raise UnicodeError()
                    texts.append(blob[0].decode("utf-8"))
                except UnicodeError:
                    result["unavailable"] = "二进制文件：显示版本大小与哈希，不提供文本差异。"
                    return result
            # ponytail: bound pathological diff cost; full snapshots remain stored.
            if sum(map(len, texts)) > 500_000:
                result["unavailable"] = "文本过大，已保存版本，但暂不生成行级差异。"
            else:
                result["diff"] = "\n".join(difflib.unified_diff(texts[0].splitlines(), texts[1].splitlines(), fromfile="修改前/" + path, tofile="修改后/" + path, lineterm=""))
        return result


def record_asset_turn(function):
    """One hook for streaming, non-streaming, and workflow worker turns."""
    @functools.wraps(function)
    async def wrapped(self, *args, **kwargs):
        root = Path(getattr(self.agent, "workdir", "") or ".")
        identifier = None
        if getattr(self.agent, "workdir", "") and root.is_dir():
            try:
                identifier = await asyncio.to_thread(begin, root, str(getattr(self.agent, "session_id", "")))
            except Exception:
                logging.getLogger(__name__).exception("Asset baseline failed")
        stream = function(self, *args, **kwargs)
        try:
            async for message in stream:
                yield message
        finally:
            await stream.aclose()
            if identifier:
                try:
                    await asyncio.shield(asyncio.to_thread(finish, root, identifier))
                except Exception:
                    logging.getLogger(__name__).exception("Asset final snapshot failed")
    return wrapped
