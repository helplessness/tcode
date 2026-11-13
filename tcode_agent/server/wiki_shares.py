from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from ..sqlite_runtime import connect as sqlite_connect

from fastapi import HTTPException

from . import workdirs as workdir_utils


_LOCK = threading.RLock()
_TARGET_TYPES = {"page", "entity_type", "collection"}


def _now() -> float:
    return time.time()


def _json(values: Iterable[str]) -> str:
    return json.dumps(sorted({str(value).strip() for value in values if str(value).strip()}))


def _loads(value: Any) -> list[str]:
    try:
        payload = json.loads(str(value or "[]"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return sorted({str(item).strip() for item in payload if str(item).strip()})


def _safe_identifier(value: Any, field: str, *, limit: int = 240) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(400, f"{field} is required")
    if len(text) > limit or any(character in text for character in "\x00\r\n"):
        raise HTTPException(400, f"{field} is invalid")
    return text


def wiki_share_db_path(server_workdir: str) -> Path:
    configured = str(os.environ.get("TCODE_WIKI_SHARES_DB") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(server_workdir) / path
    else:
        path = Path(server_workdir) / ".tcode" / "wiki_shares.sqlite3"
    return path.resolve(strict=False)


def wiki_source_id(tenant_id: str, owner_user_id: str, workspace_id: str) -> str:
    digest = hashlib.sha256(
        f"{tenant_id}\0{owner_user_id}\0{workspace_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"wsrc_{digest}"


@dataclass(frozen=True)
class WikiShareScope:
    source_id: str
    tenant_id: str
    owner_user_id: str
    workspace_id: str
    page_ids: frozenset[str]
    page_types: frozenset[str]
    share_ids: tuple[str, ...]

    @property
    def read_only(self) -> bool:
        return True

    def permits_page(self, page: dict[str, Any]) -> bool:
        return (
            str(page.get("page_id") or "") in self.page_ids
            or str(page.get("page_type") or "") in self.page_types
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "workspace_id": self.workspace_id,
            "permission": "wiki_read",
            "read_only": True,
            "page_ids": sorted(self.page_ids),
            "page_types": sorted(self.page_types),
            "share_ids": list(self.share_ids),
        }


class WikiShareStore:
    """Central authorization index for read-only, same-tenant Wiki sharing.

    The database stores logical workspace identities only. It deliberately does
    not store filesystem paths: every read resolves the owner's workspace from
    the server-controlled tenant/user/workspace hierarchy after authorization.
    """

    def __init__(self, server_workdir: str):
        self.server_workdir = str(server_workdir)
        self.path = wiki_share_db_path(server_workdir)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        connection = sqlite_connect(self.path.as_posix(), timeout=30)
        if not existed:
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_schema(self) -> None:
        with _LOCK, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS wiki_share_collections (
                    collection_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    page_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wiki_shares (
                    share_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    grantee_user_id TEXT NOT NULL,
                    permission TEXT NOT NULL DEFAULT 'wiki_read',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revoked_at REAL,
                    UNIQUE(
                        tenant_id, owner_user_id, workspace_id,
                        target_type, target_id, grantee_user_id
                    )
                );

                CREATE TABLE IF NOT EXISTS wiki_share_events (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    share_id TEXT NOT NULL DEFAULT '',
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    grantee_user_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_wiki_shares_grantee
                    ON wiki_shares(tenant_id, grantee_user_id, status, source_id);
                CREATE INDEX IF NOT EXISTS idx_wiki_shares_owner
                    ON wiki_shares(tenant_id, owner_user_id, workspace_id, status);
                CREATE INDEX IF NOT EXISTS idx_wiki_collections_owner
                    ON wiki_share_collections(tenant_id, owner_user_id, workspace_id);
                CREATE INDEX IF NOT EXISTS idx_wiki_share_events_owner
                    ON wiki_share_events(tenant_id, owner_user_id, workspace_id, created_at);
                """
            )
            connection.commit()

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        event_type: str,
        actor_user_id: str,
        share_id: str = "",
        target_type: str = "",
        target_id: str = "",
        grantee_user_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO wiki_share_events(
                event_id,tenant_id,owner_user_id,workspace_id,event_type,
                actor_user_id,share_id,target_type,target_id,grantee_user_id,
                payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"wse_{uuid.uuid4().hex}",
                tenant_id,
                owner_user_id,
                workspace_id,
                event_type,
                actor_user_id,
                share_id,
                target_type,
                target_id,
                grantee_user_id,
                json.dumps(payload or {}, sort_keys=True),
                _now(),
            ),
        )

    @staticmethod
    def _identity(tenant_id: str, owner_user_id: str, workspace_id: str) -> tuple[str, str, str]:
        return (
            workdir_utils.sanitize_owner_id(tenant_id, workdir_utils.default_tenant_id()),
            workdir_utils.sanitize_owner_id(owner_user_id, workdir_utils.default_user_id()),
            workdir_utils.sanitize_workdir_name(workspace_id) or workdir_utils.default_workspace_id(),
        )

    def create_collection(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        name: str,
        page_ids: Iterable[str],
        description: str = "",
        actor_user_id: str,
    ) -> dict[str, Any]:
        tenant_id, owner_user_id, workspace_id = self._identity(
            tenant_id, owner_user_id, workspace_id
        )
        if actor_user_id != owner_user_id:
            raise HTTPException(403, "Only the Wiki workspace owner can manage collections")
        normalized_name = str(name or "").strip()[:160]
        identifiers = [_safe_identifier(item, "page_id") for item in page_ids]
        if not normalized_name:
            raise HTTPException(400, "Collection name is required")
        if not identifiers:
            raise HTTPException(400, "A collection must contain at least one page")
        collection_id = f"wcol_{uuid.uuid4().hex}"
        now = _now()
        with _LOCK, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO wiki_share_collections(
                    collection_id,tenant_id,owner_user_id,workspace_id,name,
                    description,page_ids_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    collection_id,
                    tenant_id,
                    owner_user_id,
                    workspace_id,
                    normalized_name,
                    str(description or "").strip()[:2000],
                    _json(identifiers),
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                event_type="collection_created",
                actor_user_id=actor_user_id,
                target_type="collection",
                target_id=collection_id,
                payload={"name": normalized_name, "page_ids": sorted(set(identifiers))},
            )
            connection.commit()
        return self.get_collection(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
        )

    def get_collection(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        collection_id: str,
    ) -> dict[str, Any]:
        with _LOCK, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM wiki_share_collections
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=? AND collection_id=?
                """,
                (tenant_id, owner_user_id, workspace_id, collection_id),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Wiki collection not found")
        result = dict(row)
        result["page_ids"] = _loads(result.pop("page_ids_json", "[]"))
        return result

    def list_collections(
        self, *, tenant_id: str, owner_user_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        tenant_id, owner_user_id, workspace_id = self._identity(
            tenant_id, owner_user_id, workspace_id
        )
        with _LOCK, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM wiki_share_collections
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                ORDER BY updated_at DESC
                """,
                (tenant_id, owner_user_id, workspace_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["page_ids"] = _loads(item.pop("page_ids_json", "[]"))
            result.append(item)
        return result

    def update_collection(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        collection_id: str,
        actor_user_id: str,
        name: str,
        page_ids: Iterable[str],
        description: str = "",
    ) -> dict[str, Any]:
        if actor_user_id != owner_user_id:
            raise HTTPException(403, "Only the Wiki workspace owner can manage collections")
        collection = self.get_collection(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
        )
        normalized_name = str(name or collection["name"]).strip()[:160]
        identifiers = [_safe_identifier(item, "page_id") for item in page_ids]
        if not normalized_name or not identifiers:
            raise HTTPException(400, "Collection name and at least one page are required")
        with _LOCK, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE wiki_share_collections
                SET name=?,description=?,page_ids_json=?,updated_at=?
                WHERE collection_id=?
                """,
                (
                    normalized_name,
                    str(description or "").strip()[:2000],
                    _json(identifiers),
                    _now(),
                    collection_id,
                ),
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                event_type="collection_updated",
                actor_user_id=actor_user_id,
                target_type="collection",
                target_id=collection_id,
                payload={"name": normalized_name, "page_ids": sorted(set(identifiers))},
            )
            connection.commit()
        return self.get_collection(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
        )

    def delete_collection(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        collection_id: str,
        actor_user_id: str,
    ) -> None:
        if actor_user_id != owner_user_id:
            raise HTTPException(403, "Only the Wiki workspace owner can manage collections")
        self.get_collection(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
        )
        now = _now()
        with _LOCK, closing(self._connect()) as connection:
            shares = connection.execute(
                """
                SELECT share_id,grantee_user_id FROM wiki_shares
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                  AND target_type='collection' AND target_id=? AND status='active'
                """,
                (tenant_id, owner_user_id, workspace_id, collection_id),
            ).fetchall()
            connection.execute(
                """
                UPDATE wiki_shares SET status='revoked',revoked_at=?,updated_at=?
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                  AND target_type='collection' AND target_id=? AND status='active'
                """,
                (now, now, tenant_id, owner_user_id, workspace_id, collection_id),
            )
            connection.execute(
                "DELETE FROM wiki_share_collections WHERE collection_id=?",
                (collection_id,),
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                event_type="collection_deleted",
                actor_user_id=actor_user_id,
                target_type="collection",
                target_id=collection_id,
                payload={"revoked_share_ids": [str(row["share_id"]) for row in shares]},
            )
            connection.commit()

    def grant(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        target_type: str,
        target_id: str,
        grantee_user_ids: Iterable[str],
        actor_user_id: str,
    ) -> list[dict[str, Any]]:
        tenant_id, owner_user_id, workspace_id = self._identity(
            tenant_id, owner_user_id, workspace_id
        )
        if actor_user_id != owner_user_id:
            raise HTTPException(403, "Only the Wiki workspace owner can grant access")
        normalized_type = str(target_type or "").strip()
        if normalized_type not in _TARGET_TYPES:
            raise HTTPException(400, "target_type must be page, entity_type or collection")
        normalized_target = _safe_identifier(target_id, "target_id")
        grantees = sorted(
            {
                workdir_utils.sanitize_owner_id(item, "")
                for item in grantee_user_ids
                if workdir_utils.sanitize_owner_id(item, "")
                and workdir_utils.sanitize_owner_id(item, "") != owner_user_id
            }
        )
        if not grantees:
            raise HTTPException(400, "At least one other tenant member is required")
        if normalized_type == "collection":
            self.get_collection(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                collection_id=normalized_target,
            )
        source_id = wiki_source_id(tenant_id, owner_user_id, workspace_id)
        now = _now()
        share_ids: list[str] = []
        with _LOCK, closing(self._connect()) as connection:
            for grantee in grantees:
                share_id = f"wshr_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO wiki_shares(
                        share_id,source_id,tenant_id,owner_user_id,workspace_id,
                        target_type,target_id,grantee_user_id,permission,status,
                        created_by,created_at,updated_at,revoked_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?,NULL)
                    ON CONFLICT(
                        tenant_id,owner_user_id,workspace_id,target_type,target_id,grantee_user_id
                    ) DO UPDATE SET
                        source_id=excluded.source_id,permission='wiki_read',status='active',
                        created_by=excluded.created_by,updated_at=excluded.updated_at,revoked_at=NULL
                    """,
                    (
                        share_id,
                        source_id,
                        tenant_id,
                        owner_user_id,
                        workspace_id,
                        normalized_type,
                        normalized_target,
                        grantee,
                        "wiki_read",
                        actor_user_id,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT share_id FROM wiki_shares
                    WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                      AND target_type=? AND target_id=? AND grantee_user_id=?
                    """,
                    (
                        tenant_id,
                        owner_user_id,
                        workspace_id,
                        normalized_type,
                        normalized_target,
                        grantee,
                    ),
                ).fetchone()
                actual_share_id = str(row["share_id"])
                share_ids.append(actual_share_id)
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    workspace_id=workspace_id,
                    event_type="share_granted",
                    actor_user_id=actor_user_id,
                    share_id=actual_share_id,
                    target_type=normalized_type,
                    target_id=normalized_target,
                    grantee_user_id=grantee,
                )
            connection.commit()
        return self.list_owner_shares(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            active_only=True,
            share_ids=share_ids,
        )

    def revoke(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        share_id: str,
        actor_user_id: str,
    ) -> None:
        if actor_user_id != owner_user_id:
            raise HTTPException(403, "Only the Wiki workspace owner can revoke access")
        now = _now()
        with _LOCK, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM wiki_shares
                WHERE share_id=? AND tenant_id=? AND owner_user_id=? AND workspace_id=?
                """,
                (share_id, tenant_id, owner_user_id, workspace_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Wiki share not found")
            connection.execute(
                """
                UPDATE wiki_shares SET status='revoked',revoked_at=?,updated_at=?
                WHERE share_id=?
                """,
                (now, now, share_id),
            )
            self._event(
                connection,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                event_type="share_revoked",
                actor_user_id=actor_user_id,
                share_id=share_id,
                target_type=str(row["target_type"]),
                target_id=str(row["target_id"]),
                grantee_user_id=str(row["grantee_user_id"]),
            )
            connection.commit()

    def list_owner_shares(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        active_only: bool = True,
        share_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?", "owner_user_id=?", "workspace_id=?"]
        values: list[Any] = [tenant_id, owner_user_id, workspace_id]
        if active_only:
            conditions.append("status='active'")
        selected_ids = [str(item) for item in (share_ids or []) if str(item)]
        if selected_ids:
            conditions.append(f"share_id IN ({','.join('?' for _ in selected_ids)})")
            values.extend(selected_ids)
        with _LOCK, closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM wiki_shares WHERE {' AND '.join(conditions)} "
                "ORDER BY updated_at DESC",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_accessible_sources(
        self, *, tenant_id: str, grantee_user_id: str
    ) -> list[WikiShareScope]:
        with _LOCK, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM wiki_shares
                WHERE tenant_id=? AND grantee_user_id=? AND status='active'
                ORDER BY source_id,updated_at DESC
                """,
                (tenant_id, grantee_user_id),
            ).fetchall()
            collections = {
                (
                    str(row["collection_id"]),
                    str(row["owner_user_id"]),
                    str(row["workspace_id"]),
                ): _loads(row["page_ids_json"])
                for row in connection.execute(
                    """
                    SELECT collection_id,owner_user_id,workspace_id,page_ids_json
                    FROM wiki_share_collections
                    WHERE tenant_id=?
                    """,
                    (tenant_id,),
                ).fetchall()
            }
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            source_id = str(row["source_id"])
            item = grouped.setdefault(
                source_id,
                {
                    "tenant_id": str(row["tenant_id"]),
                    "owner_user_id": str(row["owner_user_id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "page_ids": set(),
                    "page_types": set(),
                    "share_ids": [],
                },
            )
            target_type = str(row["target_type"])
            target_id = str(row["target_id"])
            if target_type == "page":
                item["page_ids"].add(target_id)
            elif target_type == "entity_type":
                item["page_types"].add(target_id)
            elif target_type == "collection":
                item["page_ids"].update(
                    collections.get(
                        (
                            target_id,
                            str(row["owner_user_id"]),
                            str(row["workspace_id"]),
                        ),
                        [],
                    )
                )
            item["share_ids"].append(str(row["share_id"]))
        return [
            WikiShareScope(
                source_id=source_id,
                tenant_id=item["tenant_id"],
                owner_user_id=item["owner_user_id"],
                workspace_id=item["workspace_id"],
                page_ids=frozenset(item["page_ids"]),
                page_types=frozenset(item["page_types"]),
                share_ids=tuple(sorted(item["share_ids"])),
            )
            for source_id, item in grouped.items()
            if item["page_ids"] or item["page_types"]
        ]

    def resolve_source(
        self, *, tenant_id: str, grantee_user_id: str, source_id: str
    ) -> WikiShareScope:
        normalized = _safe_identifier(source_id, "source_id")
        for scope in self.list_accessible_sources(
            tenant_id=tenant_id, grantee_user_id=grantee_user_id
        ):
            if scope.source_id == normalized:
                return scope
        # Deliberately do not reveal whether the source exists for another user.
        raise HTTPException(404, "Shared Wiki source not found or no longer accessible")

    def purge_page_references(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        workspace_id: str,
        page_id: str,
        actor_user_id: str,
    ) -> dict[str, int]:
        """Revoke direct grants and remove a purged page from share collections."""

        tenant_id, owner_user_id, workspace_id = self._identity(
            tenant_id, owner_user_id, workspace_id
        )
        if actor_user_id != owner_user_id:
            raise HTTPException(403, "Only the Wiki workspace owner can purge page access")
        normalized_page_id = _safe_identifier(page_id, "page_id")
        now = _now()
        revoked_direct_share_ids: list[str] = []
        revoked_collection_share_ids: list[str] = []
        updated_collection_ids: list[str] = []
        deleted_collection_ids: list[str] = []

        with _LOCK, closing(self._connect()) as connection:
            direct_shares = connection.execute(
                """
                SELECT share_id,grantee_user_id FROM wiki_shares
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                  AND target_type='page' AND target_id=? AND status='active'
                """,
                (tenant_id, owner_user_id, workspace_id, normalized_page_id),
            ).fetchall()
            revoked_direct_share_ids = [str(row["share_id"]) for row in direct_shares]
            connection.execute(
                """
                UPDATE wiki_shares SET status='revoked',revoked_at=?,updated_at=?
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                  AND target_type='page' AND target_id=? AND status='active'
                """,
                (
                    now,
                    now,
                    tenant_id,
                    owner_user_id,
                    workspace_id,
                    normalized_page_id,
                ),
            )
            for row in direct_shares:
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    workspace_id=workspace_id,
                    event_type="share_revoked",
                    actor_user_id=actor_user_id,
                    share_id=str(row["share_id"]),
                    target_type="page",
                    target_id=normalized_page_id,
                    grantee_user_id=str(row["grantee_user_id"]),
                    payload={"reason": "page_purged"},
                )

            collections = connection.execute(
                """
                SELECT * FROM wiki_share_collections
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                """,
                (tenant_id, owner_user_id, workspace_id),
            ).fetchall()
            for collection in collections:
                page_ids = _loads(collection["page_ids_json"])
                if normalized_page_id not in page_ids:
                    continue
                collection_id = str(collection["collection_id"])
                remaining_page_ids = [
                    item for item in page_ids if item != normalized_page_id
                ]
                if remaining_page_ids:
                    connection.execute(
                        "UPDATE wiki_share_collections SET page_ids_json=?,updated_at=? "
                        "WHERE collection_id=?",
                        (_json(remaining_page_ids), now, collection_id),
                    )
                    updated_collection_ids.append(collection_id)
                    self._event(
                        connection,
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        workspace_id=workspace_id,
                        event_type="collection_updated",
                        actor_user_id=actor_user_id,
                        target_type="collection",
                        target_id=collection_id,
                        payload={
                            "reason": "page_purged",
                            "page_ids": remaining_page_ids,
                        },
                    )
                    continue

                collection_shares = connection.execute(
                    """
                    SELECT share_id FROM wiki_shares
                    WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                      AND target_type='collection' AND target_id=? AND status='active'
                    """,
                    (tenant_id, owner_user_id, workspace_id, collection_id),
                ).fetchall()
                revoked_collection_share_ids.extend(
                    str(item["share_id"]) for item in collection_shares
                )
                connection.execute(
                    """
                    UPDATE wiki_shares SET status='revoked',revoked_at=?,updated_at=?
                    WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                      AND target_type='collection' AND target_id=? AND status='active'
                    """,
                    (now, now, tenant_id, owner_user_id, workspace_id, collection_id),
                )
                connection.execute(
                    "DELETE FROM wiki_share_collections WHERE collection_id=?",
                    (collection_id,),
                )
                deleted_collection_ids.append(collection_id)
                self._event(
                    connection,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    workspace_id=workspace_id,
                    event_type="collection_deleted",
                    actor_user_id=actor_user_id,
                    target_type="collection",
                    target_id=collection_id,
                    payload={
                        "reason": "last_page_purged",
                        "revoked_share_ids": [
                            str(item["share_id"]) for item in collection_shares
                        ],
                    },
                )

            self._event(
                connection,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                event_type="page_access_purged",
                actor_user_id=actor_user_id,
                target_type="page",
                target_id=normalized_page_id,
                payload={
                    "revoked_direct_share_ids": revoked_direct_share_ids,
                    "updated_collection_ids": updated_collection_ids,
                    "deleted_collection_ids": deleted_collection_ids,
                },
            )
            connection.commit()

        return {
            "revoked_direct_shares": len(revoked_direct_share_ids),
            "revoked_collection_shares": len(revoked_collection_share_ids),
            "updated_collections": len(updated_collection_ids),
            "deleted_empty_collections": len(deleted_collection_ids),
        }

    def delete_workspace_data(
        self, *, tenant_id: str, owner_user_id: str, workspace_id: str
    ) -> dict[str, int]:
        with _LOCK, closing(self._connect()) as connection:
            share_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM wiki_shares
                    WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                    """,
                    (tenant_id, owner_user_id, workspace_id),
                ).fetchone()[0]
            )
            collection_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM wiki_share_collections
                    WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                    """,
                    (tenant_id, owner_user_id, workspace_id),
                ).fetchone()[0]
            )
            event_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM wiki_share_events
                    WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                    """,
                    (tenant_id, owner_user_id, workspace_id),
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM wiki_shares WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?",
                (tenant_id, owner_user_id, workspace_id),
            )
            connection.execute(
                """
                DELETE FROM wiki_share_collections
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                """,
                (tenant_id, owner_user_id, workspace_id),
            )
            connection.execute(
                """
                DELETE FROM wiki_share_events
                WHERE tenant_id=? AND owner_user_id=? AND workspace_id=?
                """,
                (tenant_id, owner_user_id, workspace_id),
            )
            connection.commit()
        return {
            "shares": share_count,
            "collections": collection_count,
            "events": event_count,
        }

    def delete_member_data(self, *, tenant_id: str, user_id: str) -> dict[str, int]:
        with _LOCK, closing(self._connect()) as connection:
            received = int(
                connection.execute(
                    "SELECT COUNT(*) FROM wiki_shares WHERE tenant_id=? AND grantee_user_id=?",
                    (tenant_id, user_id),
                ).fetchone()[0]
            )
            owned = int(
                connection.execute(
                    "SELECT COUNT(*) FROM wiki_shares WHERE tenant_id=? AND owner_user_id=?",
                    (tenant_id, user_id),
                ).fetchone()[0]
            )
            collections = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM wiki_share_collections
                    WHERE tenant_id=? AND owner_user_id=?
                    """,
                    (tenant_id, user_id),
                ).fetchone()[0]
            )
            connection.execute(
                """
                DELETE FROM wiki_shares
                WHERE tenant_id=? AND (owner_user_id=? OR grantee_user_id=?)
                """,
                (tenant_id, user_id, user_id),
            )
            connection.execute(
                "DELETE FROM wiki_share_collections WHERE tenant_id=? AND owner_user_id=?",
                (tenant_id, user_id),
            )
            connection.commit()
        return {"received_shares": received, "owned_shares": owned, "collections": collections}

    def delete_tenant_data(self, tenant_id: str) -> dict[str, int]:
        with _LOCK, closing(self._connect()) as connection:
            shares = int(
                connection.execute(
                    "SELECT COUNT(*) FROM wiki_shares WHERE tenant_id=?", (tenant_id,)
                ).fetchone()[0]
            )
            collections = int(
                connection.execute(
                    "SELECT COUNT(*) FROM wiki_share_collections WHERE tenant_id=?", (tenant_id,)
                ).fetchone()[0]
            )
            events = int(
                connection.execute(
                    "SELECT COUNT(*) FROM wiki_share_events WHERE tenant_id=?", (tenant_id,)
                ).fetchone()[0]
            )
            connection.execute("DELETE FROM wiki_shares WHERE tenant_id=?", (tenant_id,))
            connection.execute(
                "DELETE FROM wiki_share_collections WHERE tenant_id=?", (tenant_id,)
            )
            connection.execute("DELETE FROM wiki_share_events WHERE tenant_id=?", (tenant_id,))
            connection.commit()
        return {"shares": shares, "collections": collections, "events": events}
