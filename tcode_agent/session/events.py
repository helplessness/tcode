"""Session Canonical Event Store、投影、重放与恢复工具。"""

from __future__ import annotations

import hashlib
import base64
import gzip
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from ..file_lock import lock_state_file as lock_file, state_thread_lock, unlock_file


SESSION_EVENT_SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_FILE_LOCK_STATE = threading.local()
_OWNER_FIELDS = ("tenant_id", "user_id", "workspace_id")


def _event_compact_bytes() -> int:
    try:
        return max(0, int(os.environ.get("TCODE_SESSION_EVENT_COMPACT_BYTES", "20971520")))
    except ValueError:
        return 20 * 1024 * 1024


def _event_archive_keep() -> int:
    try:
        return max(0, int(os.environ.get("TCODE_SESSION_EVENT_ARCHIVE_KEEP", "8")))
    except ValueError:
        return 8


class SessionEventIntegrityError(RuntimeError):
    """表示事件序列、摘要或 Owner 信息无法通过完整性检查。"""


class SessionEventStore:
    """以每 Session 一个 JSONL 文件保存追加式权威事件。

    每条新事件都带单调 sequence、Owner、因果标识、幂等键和内容摘要。
    SQLite Session 行是查询投影；崩溃恢复可以从最近的 ``session.snapshot``
    事件重建，不需要调用模型或任何外部服务。
    """

    @classmethod
    def _base_dir(
        cls,
        workdir: Optional[str] = None,
        *,
        create: bool = True,
    ) -> Path:
        """返回事件根目录；只读探测可禁止创建已经删除的 Workspace。"""

        base = workdir or os.environ.get("TCODE_WORKDIR", os.getcwd())
        path = Path(base) / ".tcode" / "session_events"
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _safe_session_id(session_id: str) -> str:
        """把 Session ID 规范化为不会逃逸事件目录的文件名。"""

        normalized = str(session_id or "").strip() or "unknown-session"
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized)[:180]

    @classmethod
    def path_for(
        cls,
        session_id: str,
        workdir: Optional[str] = None,
        *,
        create: bool = True,
    ) -> Path:
        """返回指定 Session 的 Canonical Event Log 路径。"""

        return cls._base_dir(workdir, create=create) / (
            f"{cls._safe_session_id(session_id)}.jsonl"
        )

    @classmethod
    def _transaction_path(
        cls,
        session_id: str,
        workdir: Optional[str] = None,
    ) -> Path:
        return cls.path_for(session_id, workdir=workdir).with_suffix(".jsonl.txn")

    @classmethod
    @contextmanager
    def _process_lock(
        cls,
        session_id: str,
        *,
        workdir: Optional[str] = None,
    ):
        """跨进程串行化同一 Session；同线程嵌套调用复用同一把文件锁。"""

        lock_path = cls.path_for(session_id, workdir=workdir).with_suffix(".jsonl.lock")
        key = str(lock_path)
        held = getattr(_FILE_LOCK_STATE, "held", None)
        if held is None:
            held = {}
            _FILE_LOCK_STATE.held = held
        existing = held.get(key)
        if existing is not None:
            existing[1] += 1
            try:
                yield
            finally:
                existing[1] -= 1
            return

        handle = lock_path.open("a+b")
        acquired = False
        try:
            lock_file(handle)
            acquired = True
            held[key] = [handle, 1]
            yield
        finally:
            held.pop(key, None)
            try:
                if acquired:
                    unlock_file(handle)
            finally:
                handle.close()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """尽力把目录项刷盘；部分平台不支持时不影响主写入。"""

        try:
            descriptor = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    @classmethod
    def _append_transaction_bytes(cls, path: Path, payload: bytes) -> None:
        """追加一个完整批次。独立方法便于故障注入验证恢复能力。"""

        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def _recover_pending_transaction(
        cls,
        session_id: str,
        *,
        workdir: Optional[str] = None,
    ) -> None:
        """发现未提交完的批次时，恢复为“整批存在”，绝不保留半批事件。"""

        transaction_path = cls._transaction_path(session_id, workdir=workdir)
        if not transaction_path.exists():
            return
        try:
            journal = json.loads(transaction_path.read_text(encoding="utf-8"))
            base_size = int(journal.get("base_size", -1))
            base_checksum = str(journal.get("base_checksum") or "")
            batch = base64.b64decode(str(journal.get("batch_base64") or ""), validate=True)
            batch_checksum = str(journal.get("batch_checksum") or "")
        except Exception as exc:
            raise SessionEventIntegrityError("session event transaction journal is invalid") from exc
        if base_size < 0 or hashlib.sha256(batch).hexdigest() != batch_checksum:
            raise SessionEventIntegrityError("session event transaction journal checksum mismatch")

        path = cls.path_for(session_id, workdir=workdir)
        current = path.read_bytes() if path.exists() else b""
        if len(current) < base_size:
            raise SessionEventIntegrityError("session event log is shorter than transaction base")
        if hashlib.sha256(current[:base_size]).hexdigest() != base_checksum:
            raise SessionEventIntegrityError("session event transaction base mismatch")
        suffix = current[base_size:]
        if suffix.startswith(batch):
            transaction_path.unlink(missing_ok=True)
            cls._fsync_directory(transaction_path.parent)
            return
        if suffix and not batch.startswith(suffix):
            raise SessionEventIntegrityError("session event transaction tail mismatch")

        with path.open("r+b" if path.exists() else "wb") as handle:
            handle.truncate(base_size)
            handle.flush()
            os.fsync(handle.fileno())
        cls._append_transaction_bytes(path, batch)
        transaction_path.unlink(missing_ok=True)
        cls._fsync_directory(transaction_path.parent)

    @classmethod
    def session_ids(cls, *, workdir: Optional[str] = None) -> list[str]:
        """列出事件根目录中拥有 Canonical Log 的安全 Session ID。"""

        return sorted(
            path.stem
            for path in cls._base_dir(workdir).glob("*.jsonl")
            if path.is_file()
        )

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        """按稳定键顺序编码 JSON，供摘要和落盘共同使用。"""

        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def _checksum(cls, event: dict[str, Any]) -> str:
        """计算不包含 checksum 字段的事件内容摘要。"""

        payload = {key: value for key, value in event.items() if key != "checksum"}
        return hashlib.sha256(cls._json_bytes(payload)).hexdigest()

    @staticmethod
    def _normalize_owner(
        owner: Optional[dict[str, Any]],
        metadata: Optional[dict[str, Any]],
    ) -> dict[str, str]:
        """从显式 Owner 或元数据中提取固定三元组。"""

        explicit = dict(owner or {})
        fallback = dict(metadata or {})
        return {
            field: str(explicit.get(field) or fallback.get(field) or "").strip()[:128]
            for field in _OWNER_FIELDS
        }

    @classmethod
    def _read_raw(
        cls,
        session_id: str,
        *,
        workdir: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """读取可解析的原始事件，不在这里隐藏完整性错误。"""

        with state_thread_lock(_LOCK), cls._process_lock(session_id, workdir=workdir):
            cls._recover_pending_transaction(session_id, workdir=workdir)
            path = cls.path_for(session_id, workdir=workdir)
            if not path.exists():
                return []
            events: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except Exception as exc:
                        raise SessionEventIntegrityError(
                            f"session event line {line_number} is not valid JSON"
                        ) from exc
                    if not isinstance(event, dict):
                        raise SessionEventIntegrityError(
                            f"session event line {line_number} is not an object"
                        )
                    events.append(event)
            return events

    @classmethod
    def append(
        cls,
        *,
        session_id: str,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        owner: Optional[dict[str, Any]] = None,
        causation_id: str = "",
        idempotency_key: str = "",
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        """幂等追加一条带完整性信息的 Session 事件。

        同一 Session 内重复的非空幂等键返回已有事件；Owner 一旦出现非空值，
        后续事件不得用不同值覆盖，从而避免跨租户日志拼接。
        """

        events = cls.append_batch(
            session_id=session_id,
            events=[
                {
                    "event_type": event_type,
                    "payload": payload,
                    "metadata": metadata,
                    "causation_id": causation_id,
                    "idempotency_key": idempotency_key,
                }
            ],
            owner=owner,
            workdir=workdir,
        )
        return events[0]

    @classmethod
    def append_batch(
        cls,
        *,
        session_id: str,
        events: list[dict[str, Any]],
        owner: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """用落盘事务日志整批追加相关事件，崩溃后自动补齐或回滚半批。"""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id is required")
        specifications = [dict(item or {}) for item in list(events or [])]
        if not specifications:
            return []
        with state_thread_lock(_LOCK), cls._process_lock(normalized_session_id, workdir=workdir):
            existing = cls._read_raw(normalized_session_id, workdir=workdir)
            existing_by_key = {
                str(item.get("idempotency_key") or ""): item
                for item in existing
                if str(item.get("idempotency_key") or "")
            }
            normalized_keys = [
                str(item.get("idempotency_key") or "").strip()[:240]
                for item in specifications
            ]
            found = [key in existing_by_key for key in normalized_keys if key]
            if found and all(found) and len(found) == len(specifications):
                return [dict(existing_by_key[key]) for key in normalized_keys]
            if any(found):
                raise SessionEventIntegrityError(
                    "session event transaction was only partially committed"
                )

            normalized_owner = cls._normalize_owner(
                owner,
                next(
                    (
                        item.get("metadata")
                        for item in specifications
                        if isinstance(item.get("metadata"), dict)
                    ),
                    None,
                ),
            )
            for existing_event in existing:
                event_owner = existing_event.get("owner")
                if not isinstance(event_owner, dict):
                    continue
                for field in _OWNER_FIELDS:
                    previous = str(event_owner.get(field) or "").strip()
                    current = normalized_owner[field]
                    if previous and current and previous != current:
                        raise SessionEventIntegrityError(
                            f"session event owner mismatch for {field}"
                        )

            transaction_id = f"txn-{uuid.uuid4()}"
            created_at = time.time()
            prepared: list[dict[str, Any]] = []
            for index, specification in enumerate(specifications, start=1):
                event = {
                    "schema_version": SESSION_EVENT_SCHEMA_VERSION,
                    "event_id": f"evt-{uuid.uuid4()}",
                    "session_id": normalized_session_id,
                    "sequence": len(existing) + index,
                    "transaction_id": transaction_id,
                    "transaction_index": index,
                    "transaction_size": len(specifications),
                    "event_type": str(specification.get("event_type") or "unknown").strip()
                    or "unknown",
                    "created_at": created_at,
                    "owner": normalized_owner,
                    "causation_id": str(specification.get("causation_id") or "").strip()[:240],
                    "idempotency_key": normalized_keys[index - 1],
                    "payload": dict(specification.get("payload") or {}),
                    "metadata": dict(specification.get("metadata") or {}),
                }
                event["checksum"] = cls._checksum(event)
                prepared.append(event)

            path = cls.path_for(normalized_session_id, workdir=workdir)
            batch = b"".join(cls._json_bytes(event) + b"\n" for event in prepared)
            current = path.read_bytes() if path.exists() else b""
            transaction_path = cls._transaction_path(
                normalized_session_id,
                workdir=workdir,
            )
            journal = {
                "schema_version": 1,
                "session_id": normalized_session_id,
                "transaction_id": transaction_id,
                "base_size": len(current),
                "base_checksum": hashlib.sha256(current).hexdigest(),
                "batch_checksum": hashlib.sha256(batch).hexdigest(),
                "batch_base64": base64.b64encode(batch).decode("ascii"),
            }
            temporary_path = transaction_path.with_name(
                f"{transaction_path.name}.tmp-{uuid.uuid4().hex}"
            )
            with temporary_path.open("wb") as handle:
                handle.write(cls._json_bytes(journal))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, transaction_path)
            cls._fsync_directory(transaction_path.parent)
            cls._append_transaction_bytes(path, batch)
            transaction_path.unlink(missing_ok=True)
            cls._fsync_directory(transaction_path.parent)
            return prepared

    @classmethod
    def record_snapshot(
        cls,
        session_id: str,
        *,
        snapshot: dict[str, Any],
        owner: Optional[dict[str, Any]] = None,
        related_events: Optional[list[dict[str, Any]]] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        """把完整 Session 状态压缩为首个 Snapshot 或最小尾部 Patch 事件。

        消息列表使用最长公共前缀，只记录发生变化位置之后的尾部；普通字段只
        记录变化键。这样 Event Log 仍可独立重放，同时避免每个 Turn 阶段重复
        保存整段历史消息。
        """

        with state_thread_lock(_LOCK), cls._process_lock(session_id, workdir=workdir):
            normalized_snapshot = dict(snapshot or {})
            digest = hashlib.sha256(cls._json_bytes(normalized_snapshot)).hexdigest()
            projection = cls.project(session_id, workdir=workdir)
            previous = projection.get("session")
            snapshot_event: Optional[dict[str, Any]] = None
            if projection.get("deleted"):
                # 删除是终态。迟到的 Worker 或旧进程不能通过保存快照复活
                # Session；related_events 仍可作为删除后的审计事件追加。
                snapshot_event = None
            elif not isinstance(previous, dict) or not previous:
                snapshot_event = {
                    "event_type": "session.snapshot",
                    "payload": {"session": normalized_snapshot},
                    "idempotency_key": f"snapshot:{digest}",
                }

            if snapshot_event is None and not projection.get("deleted"):
                previous_messages = list(previous.get("messages") or [])
                next_messages = list(normalized_snapshot.get("messages") or [])
                common_prefix = 0
                for previous_item, next_item in zip(previous_messages, next_messages):
                    if previous_item != next_item:
                        break
                    common_prefix += 1

                fields = {
                    key: value
                    for key, value in normalized_snapshot.items()
                    if key != "messages" and previous.get(key) != value
                }
                removed_fields = sorted(
                    key
                    for key in previous
                    if key not in normalized_snapshot and key != "messages"
                )
                messages_changed = previous_messages != next_messages
                if fields or removed_fields or messages_changed:
                    payload: dict[str, Any] = {
                        "fields": fields,
                        "removed_fields": removed_fields,
                    }
                    if messages_changed:
                        payload["messages_from"] = common_prefix
                        payload["messages"] = next_messages[common_prefix:]
                    snapshot_event = {
                        "event_type": "session.patch",
                        "payload": payload,
                        "idempotency_key": f"snapshot:{digest}",
                    }

            batch = ([snapshot_event] if snapshot_event else []) + [
                dict(item or {}) for item in list(related_events or [])
            ]
            if batch:
                appended = cls.append_batch(
                    session_id=session_id,
                    events=batch,
                    owner=owner,
                    workdir=workdir,
                )
                threshold = _event_compact_bytes()
                path = cls.path_for(session_id, workdir=workdir)
                if threshold and path.exists() and path.stat().st_size >= threshold:
                    cls.compact(
                        session_id,
                        snapshot=normalized_snapshot,
                        owner=owner,
                        workdir=workdir,
                    )
                return appended[0]
            events = cls.list(session_id, workdir=workdir, limit=1)
            return dict(events[-1]) if events else {}

    @classmethod
    def list(
        cls,
        session_id: str,
        *,
        workdir: Optional[str] = None,
        limit: Optional[int] = None,
        after_sequence: int = 0,
        verify: bool = True,
    ) -> list[dict[str, Any]]:
        """按 sequence 返回事件，并可执行摘要与连续性校验。"""

        events = cls._read_raw(session_id, workdir=workdir)
        if verify:
            cls.verify(events, expected_session_id=session_id)
        filtered = [
            dict(event)
            for event in events
            if int(event.get("sequence", 0) or 0) > max(0, int(after_sequence))
        ]
        if limit is not None:
            return filtered[-max(0, int(limit)) :]
        return filtered

    @classmethod
    def verify(
        cls,
        events: list[dict[str, Any]],
        *,
        expected_session_id: str = "",
    ) -> None:
        """验证事件版本、Session、单调序列和 checksum。"""

        previous_sequence = 0
        for event in events:
            version = int(event.get("schema_version", 0) or 0)
            if version != SESSION_EVENT_SCHEMA_VERSION:
                raise SessionEventIntegrityError(f"unsupported session event schema: {version}")
            if expected_session_id and str(event.get("session_id") or "") != str(expected_session_id):
                raise SessionEventIntegrityError("session event id mismatch")
            sequence = int(event.get("sequence", 0) or 0)
            if sequence != previous_sequence + 1:
                raise SessionEventIntegrityError("session event sequence is not contiguous")
            checksum = str(event.get("checksum") or "")
            if not checksum or checksum != cls._checksum(event):
                raise SessionEventIntegrityError("session event checksum mismatch")
            previous_sequence = sequence

        index = 0
        while index < len(events):
            event = events[index]
            transaction_id = str(event.get("transaction_id") or "")
            if not transaction_id:
                index += 1
                continue
            try:
                transaction_size = int(event.get("transaction_size", 0) or 0)
            except Exception as exc:
                raise SessionEventIntegrityError("invalid session event transaction size") from exc
            if transaction_size < 1 or index + transaction_size > len(events):
                raise SessionEventIntegrityError("session event transaction is incomplete")
            group = events[index : index + transaction_size]
            if any(str(item.get("transaction_id") or "") != transaction_id for item in group):
                raise SessionEventIntegrityError("session event transaction is not contiguous")
            if [int(item.get("transaction_index", 0) or 0) for item in group] != list(
                range(1, transaction_size + 1)
            ):
                raise SessionEventIntegrityError("session event transaction indexes are invalid")
            if any(int(item.get("transaction_size", 0) or 0) != transaction_size for item in group):
                raise SessionEventIntegrityError("session event transaction sizes do not match")
            index += transaction_size

    @classmethod
    def project(
        cls,
        session_id: str,
        *,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        """从事件重放出最新 Session Snapshot 和 Turn 状态。"""

        projection: dict[str, Any] = {
            "session_id": str(session_id or ""),
            "session": {},
            "deleted": False,
            "active_turn": {},
            "last_turn": {},
            "last_sequence": 0,
        }
        for event in cls.list(session_id, workdir=workdir):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_type = str(event.get("event_type") or "")
            projection["last_sequence"] = int(event.get("sequence", 0) or 0)
            if event_type == "session.deleted":
                projection["session"] = {}
                projection["deleted"] = True
                projection["active_turn"] = {}
            elif event_type == "session.baseline" and isinstance(
                payload.get("projection"), dict
            ):
                baseline = dict(payload["projection"])
                projection.update(baseline)
                projection["session_id"] = str(session_id or "")
                projection["last_sequence"] = int(event.get("sequence", 0) or 0)
            elif projection["deleted"]:
                # Tombstone 是永久终态；兼容历史上删除后又被迟到写入的
                # snapshot/patch/turn 事件，但绝不让它们恢复查询投影。
                continue
            elif event_type == "session.snapshot" and isinstance(payload.get("session"), dict):
                projection["session"] = dict(payload["session"])
            elif event_type == "session.patch":
                current = dict(projection.get("session") or {})
                for key in list(payload.get("removed_fields") or []):
                    current.pop(str(key), None)
                if isinstance(payload.get("fields"), dict):
                    current.update(dict(payload["fields"]))
                if "messages_from" in payload and isinstance(payload.get("messages"), list):
                    start = max(0, int(payload.get("messages_from", 0) or 0))
                    current["messages"] = [
                        *list(current.get("messages") or [])[:start],
                        *list(payload["messages"]),
                    ]
                projection["session"] = current
            elif event_type == "turn.state":
                if bool((event.get("metadata") or {}).get("terminal")):
                    projection["last_turn"] = dict(payload)
                    projection["active_turn"] = {}
                else:
                    projection["active_turn"] = dict(payload)
            elif event_type.startswith("runtime.v2.") and isinstance(
                payload.get("event"), dict
            ):
                from ..protocol.v2.persistence import apply_runtime_event_projection

                projection["runtime_v2"] = apply_runtime_event_projection(
                    dict(projection.get("runtime_v2") or {}),
                    dict(payload["event"]),
                )
            elif event_type == "session.compacted":
                projection["last_compaction"] = dict(payload)
        return projection

    @classmethod
    def compact(
        cls,
        session_id: str,
        *,
        snapshot: dict[str, Any],
        owner: Optional[dict[str, Any]] = None,
        workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        """Archive the full audit log and replace the hot log with one baseline."""

        normalized_session_id = str(session_id or "").strip()
        with state_thread_lock(_LOCK), cls._process_lock(normalized_session_id, workdir=workdir):
            events = cls._read_raw(normalized_session_id, workdir=workdir)
            if not events:
                return {}
            cls.verify(events, expected_session_id=normalized_session_id)
            projection = cls.project(normalized_session_id, workdir=workdir)
            projection["session"] = dict(snapshot or projection.get("session") or {})
            covered_sequence = int(events[-1].get("sequence", 0) or 0)
            normalized_owner = cls._normalize_owner(owner, events[-1].get("metadata"))
            if not any(normalized_owner.values()):
                normalized_owner = cls._normalize_owner(events[-1].get("owner"), None)

            path = cls.path_for(normalized_session_id, workdir=workdir)
            original = path.read_bytes()
            archive_dir = cls._base_dir(workdir) / "archive" / cls._safe_session_id(
                normalized_session_id
            )
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / (
                f"{int(time.time() * 1000)}-seq-{covered_sequence}.jsonl.gz"
            )
            archive_temp = archive_path.with_suffix(".gz.tmp")
            archive_temp.write_bytes(gzip.compress(original, compresslevel=6))
            with archive_temp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(archive_temp, archive_path)
            cls._fsync_directory(archive_dir)
            from .event_archive import EventArchiveStore

            # 索引失败时不替换热日志，确保每份冷归档都可发现、可回读。
            EventArchiveStore.index(archive_path, workdir=workdir)

            transaction_id = f"txn-{uuid.uuid4()}"
            baseline = {
                "schema_version": SESSION_EVENT_SCHEMA_VERSION,
                "event_id": f"evt-{uuid.uuid4()}",
                "session_id": normalized_session_id,
                "sequence": 1,
                "transaction_id": transaction_id,
                "transaction_index": 1,
                "transaction_size": 1,
                "event_type": "session.baseline",
                "created_at": time.time(),
                "owner": normalized_owner,
                "causation_id": "",
                "idempotency_key": f"baseline:{covered_sequence}:{hashlib.sha256(original).hexdigest()}",
                "payload": {
                    "covered_sequence": covered_sequence,
                    "archive": archive_path.relative_to(cls._base_dir(workdir)).as_posix(),
                    "projection": projection,
                },
                "metadata": {"physical_compaction": True},
            }
            baseline["checksum"] = cls._checksum(baseline)
            replacement = cls._json_bytes(baseline) + b"\n"
            temporary = path.with_suffix(f".jsonl.{uuid.uuid4().hex}.tmp")
            with temporary.open("wb") as handle:
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)

            keep = _event_archive_keep()
            archives = sorted(archive_dir.glob("*.jsonl.gz"), key=lambda item: item.stat().st_mtime)
            if keep > 0:
                for stale in archives[:-keep]:
                    EventArchiveStore.remove(stale, workdir=workdir)
                    stale.unlink(missing_ok=True)
            return baseline


def session_event_contract_document() -> dict[str, Any]:
    """返回第三批 Session Event 单轨合同说明。"""

    return {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "contract_id": "tcode.session-events.v1",
        "canonical_source": "append-only-session-event-log",
        "projection_store": "sqlite",
        "owner_fields": list(_OWNER_FIELDS),
        "sequence": "strictly-monotonic-per-session",
        "atomic_batches": True,
        "crash_recovery": "write-ahead-transaction-journal",
        "cross_process_locking": True,
        "idempotency": True,
        "checksum": "sha256",
        "replay_external_side_effects": False,
        "compaction_preserves_audit_chain": True,
        "delete_semantics": "owner-checked-tombstone-prevents-resurrection",
        "runtime_v2_event_prefix": "runtime.v2.",
        "runtime_v2_high_frequency_deltas_persisted": False,
    }


__all__ = [
    "SESSION_EVENT_SCHEMA_VERSION",
    "SessionEventIntegrityError",
    "SessionEventStore",
    "session_event_contract_document",
]
