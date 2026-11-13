"""Owner-scoped progressive reading across durable research stores.

The service is the trusted boundary shared by prompt assembly, HTTP APIs, and
agent tools.  It keeps routing summaries bounded, exposes exact object reads,
and uses opaque cursors so knowledge volume never implies unbounded prompt or
process memory.
"""

from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from ..evidence import EvidenceStore
from .config import ResearchSettings, load_research_settings
from .episode_models import ResearchConflict, ResearchEpisode, ResearchFinding
from .episode_store import ResearchDerivationStore
from .experience_store import ResearchExperienceStore
from .models import canonical_json, sha256_text
from .pack_asset_store import ResearchPackAssetStore
from .document_index import ResearchPackDocumentIndex
from .topic_store import ResearchTopicStore
from .store import ResearchEventStore


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]{2,}")
_LATIN_TERM = re.compile(r"[a-z0-9][a-z0-9_.+-]{1,}")
_STOP_TERMS = {
    "一个", "一些", "这个", "那个", "什么", "怎么", "如何", "可以", "是否", "关于",
    "现在", "之前", "之后", "我们", "你们", "他们", "用户", "问题", "内容", "当前",
    "研究", "课题", "结论", "结果", "继续", "相关", "已经", "进行",
    "the", "and", "for", "with", "from", "this", "that", "what", "how",
    "research", "project", "current", "continue", "result", "question",
}


def _clean_text(value: Any, limit: int = 4000) -> str:
    text = _CONTROL.sub("", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


def _terms(value: Any) -> set[str]:
    text = _clean_text(value, 20000).lower()
    result = set(_LATIN_TERM.findall(text))
    for run in _CJK_RUN.findall(text):
        if 2 <= len(run) <= 20:
            result.add(run)
        for size in (2, 3):
            result.update(run[index : index + size] for index in range(len(run) - size + 1))
    return {item for item in result if item not in _STOP_TERMS and len(item) >= 2}


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else canonical_json(value)
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return max(1, cjk + math.ceil(max(0, len(text) - cjk) / 4))


def derive_experience_items(
    research_id: str,
    episodes: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project durable Episode fields into stable, first-class experience items."""

    result: list[dict[str, Any]] = []
    for episode in episodes:
        groups = (
            ("decision", list(episode.get("decisions") or [])),
            ("failed_path", list(episode.get("failed_paths") or [])),
            ("method", list(episode.get("tool_calls") or [])),
        )
        for kind, values in groups:
            for position, value in enumerate(values):
                if isinstance(value, dict):
                    content = next(
                        (
                            _clean_text(value.get(key), 2000)
                            for key in ("value", "summary", "name", "reason", "error", "tool")
                            if _clean_text(value.get(key), 2000)
                        ),
                        _clean_text(canonical_json(value), 2000),
                    )
                else:
                    content = _clean_text(value, 2000)
                if not content:
                    continue
                identity = {
                    "type": kind,
                    "episode_id": episode.get("episode_id"),
                    "position": position,
                    "content": content,
                }
                result.append(
                    {
                        "experience_id": sha256_text(canonical_json(identity)),
                        "research_id": research_id,
                        "type": kind,
                        "content": content,
                        "source_episode_id": episode.get("episode_id"),
                        "source_started_at": episode.get("started_at"),
                        "outcome_status": episode.get("outcome_status"),
                        "source": value if isinstance(value, dict) else {"value": value},
                    }
                )
    return result


def _cursor_encode(kind: str, values: dict[str, Any]) -> str:
    raw = canonical_json({"v": 1, "kind": kind, **values}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(cursor: str, kind: str) -> dict[str, Any]:
    value = str(cursor or "").strip()
    if not value:
        return {}
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid research read cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("kind") != kind:
        raise ValueError("research read cursor does not match object type")
    return payload


class ResearchReadService:
    """Progressive, bounded read service for one Workspace owner."""

    MAX_PAGE_SIZE = 500
    MAX_MANDATORY_CONFLICTS = 500
    MAX_SEARCH_SCOPE_IDS = 10_000

    def __init__(
        self,
        *,
        tenant_id: str,
        user_id: str,
        workspace_id: str,
        workdir: str,
        settings: ResearchSettings | None = None,
    ):
        self.owner = {
            "tenant_id": str(tenant_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "workspace_id": str(workspace_id or "").strip(),
        }
        if not all(self.owner.values()):
            raise ValueError("complete owner context is required")
        self.workdir = Path(workdir).expanduser().resolve(strict=False)
        self.settings = settings or load_research_settings()

    def _project(self, research_id: str):
        project = ResearchTopicStore.get_project(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if project is None:
            raise ValueError("research project not found")
        return project

    def _connect(self):
        ResearchEventStore.ensure_initialized(self.workdir.as_posix(), settings=self.settings)
        return ResearchEventStore._connect(self.workdir.as_posix(), settings=self.settings)

    def _linked_object_scope(
        self,
        research_id: str,
        object_type: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return a bounded project-to-knowledge scope without silent truncation."""

        safe_limit = max(1, min(int(limit or self.MAX_SEARCH_SCOPE_IDS), self.MAX_SEARCH_SCOPE_IDS))
        params = [*self.owner.values(), research_id, str(object_type or "").strip()]
        where = (
            "tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? "
            "AND object_type=? AND superseded_by='' AND state!='removed'"
        )
        if object_type == "evidence":
            where += " AND object_id NOT LIKE 'event:%'"
        connection = self._connect()
        try:
            total = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT object_id) FROM research_knowledge_links WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT DISTINCT object_id FROM research_knowledge_links WHERE {where} "
                "ORDER BY object_id LIMIT ?",
                [*params, safe_limit],
            ).fetchall()
        finally:
            connection.close()
        ids = [str(row["object_id"] or "") for row in rows if str(row["object_id"] or "")]
        return {
            "object_type": object_type,
            "total_count": total,
            "loaded_count": len(ids),
            "complete": len(ids) == total,
            "ids": ids,
        }

    def _require_linked_object(
        self,
        research_id: str,
        object_type: str,
        object_id: str,
    ) -> None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM research_knowledge_links WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND research_id=? AND object_type=? AND object_id=? "
                "AND superseded_by='' AND state!='removed' LIMIT 1",
                [*self.owner.values(), research_id, object_type, object_id],
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError(f"{object_type} is not linked to this research project")

    @staticmethod
    def _page_limit(limit: int) -> int:
        return max(1, min(int(limit or 20), ResearchReadService.MAX_PAGE_SIZE))

    @staticmethod
    def _status_clause(statuses: Iterable[str] | None, allowed: set[str]) -> tuple[str, list[str]]:
        values = list(
            dict.fromkeys(
                str(item or "").strip().lower()
                for item in (statuses or [])
                if str(item or "").strip().lower() in allowed
            )
        )
        if not values:
            return "", []
        return " AND status IN (" + ",".join("?" for _ in values) + ")", values

    @staticmethod
    def _like_clause(columns: list[str], query: str) -> tuple[str, list[str], set[str]]:
        terms = sorted(_terms(query), key=lambda item: (-len(item), item))[:8]
        if not terms:
            return "", [], set()
        clauses: list[str] = []
        values: list[str] = []
        for term in terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns) + ")")
            values.extend([f"%{escaped}%"] * len(columns))
        return " AND (" + " OR ".join(clauses) + ")", values, set(terms)

    def counts(self, research_id: str) -> dict[str, int]:
        self._project(research_id)
        connection = self._connect()
        try:
            owner_values = [self.owner["tenant_id"], self.owner["user_id"], self.owner["workspace_id"], research_id]
            queries = {
                "episodes": "SELECT COUNT(*) FROM research_episodes WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND state='active'",
                "findings": "SELECT COUNT(*) FROM research_findings WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?",
                "open_conflicts": "SELECT COUNT(*) FROM research_conflicts WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND status IN ('open','under_review')",
                "changes": "SELECT COUNT(*) FROM research_change_events WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?",
                "knowledge_links": "SELECT COUNT(*) FROM research_knowledge_links WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND state!='removed'",
            }
            result = {
                key: int(connection.execute(sql, owner_values).fetchone()[0])
                for key, sql in queries.items()
            }
            result["experience_items"] = int(
                connection.execute(
                    "SELECT COALESCE(SUM(json_array_length(decisions_json) + "
                    "json_array_length(failed_paths_json) + json_array_length(tool_calls_json)),0) "
                    "FROM research_episodes WHERE tenant_id=? AND user_id=? AND workspace_id=? "
                    "AND research_id=? AND state='active'",
                    owner_values,
                ).fetchone()[0]
            )
            linked = connection.execute(
                "SELECT object_type,COUNT(DISTINCT object_id) AS count "
                "FROM research_knowledge_links WHERE tenant_id=? AND user_id=? "
                "AND workspace_id=? AND research_id=? AND superseded_by='' "
                "AND state!='removed' GROUP BY object_type",
                owner_values,
            ).fetchall()
            linked_counts = {str(row["object_type"]): int(row["count"] or 0) for row in linked}
            result.update(
                {
                    "evidence_records": int(
                        connection.execute(
                            "SELECT COUNT(DISTINCT object_id) FROM research_knowledge_links "
                            "WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? "
                            "AND object_type='evidence' AND object_id NOT LIKE 'event:%' "
                            "AND superseded_by='' AND state!='removed'",
                            owner_values,
                        ).fetchone()[0]
                    ),
                    "papers": linked_counts.get("paper", 0),
                    "wiki_pages": linked_counts.get("wiki_page", 0),
                    "passages": linked_counts.get("passage", 0),
                }
            )
            return result
        finally:
            connection.close()

    def list_conflicts(
        self,
        research_id: str,
        *,
        statuses: Iterable[str] | None = ("open", "under_review"),
        query: str = "",
        limit: int = 100,
        cursor: str = "",
    ) -> dict[str, Any]:
        self._project(research_id)
        safe_limit = self._page_limit(limit)
        status_sql, status_values = self._status_clause(
            statuses,
            {"open", "under_review", "resolved"},
        )
        like_sql, like_values, query_terms = self._like_clause(
            ["summary", "original_refs_json", "challenging_refs_json"],
            query,
        )
        cursor_data = _cursor_decode(cursor, "conflict")
        cursor_sql = ""
        cursor_values: list[Any] = []
        if cursor_data:
            updated_at = str(cursor_data.get("updated_at") or "")
            object_id = str(cursor_data.get("id") or "")
            cursor_sql = " AND (updated_at < ? OR (updated_at = ? AND conflict_id < ?))"
            cursor_values = [updated_at, updated_at, object_id]
        base = "tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?"
        params = [*self.owner.values(), research_id, *status_values, *like_values]
        connection = self._connect()
        try:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM research_conflicts WHERE {base}{status_sql}{like_sql}",
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM research_conflicts WHERE {base}{status_sql}{like_sql}{cursor_sql} "
                "ORDER BY updated_at DESC, conflict_id DESC LIMIT ?",
                [*params, *cursor_values, safe_limit + 1],
            ).fetchall()
        finally:
            connection.close()
        has_more = len(rows) > safe_limit
        selected = rows[:safe_limit]
        items: list[dict[str, Any]] = []
        for row in selected:
            item = ResearchConflict.from_row(row).to_dict()
            matched = query_terms & _terms(
                " ".join(
                    [
                        str(item.get("summary") or ""),
                        canonical_json(item.get("original_refs") or []),
                        canonical_json(item.get("challenging_refs") or []),
                    ]
                )
            )
            item["retrieval"] = {
                "matched_terms": sorted(matched, key=lambda value: (-len(value), value))[:20],
                "score": round(len(matched) * 2.0, 4),
            }
            items.append(item)
        if query_terms:
            items.sort(
                key=lambda item: (
                    float((item.get("retrieval") or {}).get("score") or 0),
                    item.get("updated_at", ""),
                ),
                reverse=True,
            )
        next_cursor = ""
        if has_more and selected:
            last = selected[-1]
            next_cursor = _cursor_encode(
                "conflict",
                {"updated_at": str(last["updated_at"] or ""), "id": str(last["conflict_id"] or "")},
            )
        return {
            "object_type": "conflict",
            "query": _clean_text(query, 1000),
            "total_count": total,
            "loaded_count": len(items),
            "complete": not has_more and not cursor,
            "page_complete": not has_more,
            "next_cursor": next_cursor,
            "items": items,
        }

    def list_findings(
        self,
        research_id: str,
        *,
        query: str = "",
        statuses: Iterable[str] | None = None,
        limit: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        self._project(research_id)
        safe_limit = self._page_limit(limit)
        status_sql, status_values = self._status_clause(
            statuses,
            {"active", "context_limited", "disputed", "superseded", "retracted"},
        )
        like_sql, like_values, query_terms = self._like_clause(
            ["statement", "scope_json"],
            query,
        )
        cursor_data = _cursor_decode(cursor, "finding")
        cursor_sql = ""
        cursor_values: list[Any] = []
        if cursor_data:
            updated_at = str(cursor_data.get("updated_at") or "")
            object_id = str(cursor_data.get("id") or "")
            cursor_sql = " AND (updated_at < ? OR (updated_at = ? AND finding_id < ?))"
            cursor_values = [updated_at, updated_at, object_id]
        base = "tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?"
        params = [*self.owner.values(), research_id, *status_values, *like_values]
        connection = self._connect()
        try:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM research_findings WHERE {base}{status_sql}{like_sql}",
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM research_findings WHERE {base}{status_sql}{like_sql}{cursor_sql} "
                "ORDER BY updated_at DESC, finding_id DESC LIMIT ?",
                [*params, *cursor_values, safe_limit + 1],
            ).fetchall()
        finally:
            connection.close()
        has_more = len(rows) > safe_limit
        selected = rows[:safe_limit]
        items: list[dict[str, Any]] = []
        for row in selected:
            item = ResearchFinding.from_row(row).to_dict()
            matched = query_terms & _terms(item.get("statement"))
            item["retrieval"] = {
                "matched_terms": sorted(matched, key=lambda value: (-len(value), value))[:20],
                "score": round(len(matched) + (1.5 if item.get("status") == "disputed" else 0.0), 4),
            }
            items.append(item)
        if query_terms:
            items.sort(key=lambda item: (float((item.get("retrieval") or {}).get("score") or 0), item.get("updated_at", "")), reverse=True)
        next_cursor = ""
        if has_more and selected:
            last = selected[-1]
            next_cursor = _cursor_encode(
                "finding",
                {"updated_at": str(last["updated_at"] or ""), "id": str(last["finding_id"] or "")},
            )
        return {
            "object_type": "finding",
            "query": _clean_text(query, 1000),
            "total_count": total,
            "loaded_count": len(items),
            "complete": not has_more and not cursor,
            "page_complete": not has_more,
            "next_cursor": next_cursor,
            "items": items,
        }

    def list_episodes(
        self,
        research_id: str,
        *,
        query: str = "",
        limit: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        self._project(research_id)
        safe_limit = self._page_limit(limit)
        like_sql, like_values, query_terms = self._like_clause(
            ["title", "goal_json", "summary_json", "decisions_json", "failed_paths_json", "open_questions_json", "next_actions_json"],
            query,
        )
        cursor_data = _cursor_decode(cursor, "episode")
        cursor_sql = ""
        cursor_values: list[Any] = []
        if cursor_data:
            started_at = str(cursor_data.get("started_at") or "")
            revision = int(cursor_data.get("revision") or 0)
            object_id = str(cursor_data.get("id") or "")
            cursor_sql = (
                " AND (started_at < ? OR (started_at = ? AND revision < ?) "
                "OR (started_at = ? AND revision = ? AND episode_id < ?))"
            )
            cursor_values = [started_at, started_at, revision, started_at, revision, object_id]
        base = "tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND state='active'"
        params = [*self.owner.values(), research_id, *like_values]
        connection = self._connect()
        try:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM research_episodes WHERE {base}{like_sql}",
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM research_episodes WHERE {base}{like_sql}{cursor_sql} "
                "ORDER BY started_at DESC, revision DESC, episode_id DESC LIMIT ?",
                [*params, *cursor_values, safe_limit + 1],
            ).fetchall()
            selected = rows[:safe_limit]
            ids = [str(row["episode_id"] or "") for row in selected]
            finding_counts: dict[str, int] = {}
            if ids:
                placeholders = ",".join("?" for _ in ids)
                linked = connection.execute(
                    f"SELECT episode_id,COUNT(*) AS count FROM research_findings WHERE episode_id IN ({placeholders}) "
                    "AND status IN ('active','context_limited','disputed') GROUP BY episode_id",
                    ids,
                ).fetchall()
                finding_counts = {str(row["episode_id"]): int(row["count"] or 0) for row in linked}
        finally:
            connection.close()
        has_more = len(rows) > safe_limit
        items: list[dict[str, Any]] = []
        for row in selected:
            episode = ResearchEpisode.from_row(row)
            item = episode.to_dict()
            text = " ".join(
                [
                    episode.title,
                    str(episode.goal.get("value") or ""),
                    str(episode.summary.get("value") or ""),
                    canonical_json(episode.decisions),
                    canonical_json(episode.failed_paths),
                    canonical_json(episode.open_questions),
                    canonical_json(episode.next_actions),
                ]
            )
            matched = query_terms & _terms(text)
            linked_findings = finding_counts.get(episode.episode_id, 0)
            importance = (
                min(3.0, linked_findings * 0.75)
                + (0.8 if episode.decisions else 0.0)
                + (0.6 if episode.failed_paths else 0.0)
                + (0.5 if episode.open_questions or episode.next_actions else 0.0)
                + (0.4 if episode.artifact_refs else 0.0)
            )
            item["retrieval"] = {
                "matched_terms": sorted(matched, key=lambda value: (-len(value), value))[:20],
                "linked_current_findings": linked_findings,
                "importance_score": round(importance, 4),
                "score": round(len(matched) * 2.0 + importance, 4),
            }
            items.append(item)
        if query_terms:
            items.sort(
                key=lambda item: (
                    float((item.get("retrieval") or {}).get("score") or 0),
                    item.get("started_at", ""),
                ),
                reverse=True,
            )
        next_cursor = ""
        if has_more and selected:
            last = selected[-1]
            next_cursor = _cursor_encode(
                "episode",
                {
                    "started_at": str(last["started_at"] or ""),
                    "revision": int(last["revision"] or 0),
                    "id": str(last["episode_id"] or ""),
                },
            )
        return {
            "object_type": "episode",
            "query": _clean_text(query, 1000),
            "total_count": total,
            "loaded_count": len(items),
            "complete": not has_more and not cursor,
            "page_complete": not has_more,
            "next_cursor": next_cursor,
            "items": items,
        }

    def select_episodes(self, research_id: str, question: str, *, limit: int) -> list[dict[str, Any]]:
        safe_limit = max(0, min(int(limit or 0), 20))
        if safe_limit == 0:
            return []
        recent_page = self.list_episodes(research_id, limit=max(2, safe_limit))
        recent = list(recent_page["items"])
        if safe_limit == 1 or not _terms(question):
            return recent[:safe_limit]
        salient_page = self.list_episodes(
            research_id,
            query=question,
            limit=max(20, safe_limit * 8),
        )
        selected: list[dict[str, Any]] = []
        if recent:
            selected.append(recent[0])
        seen = {str(item.get("episode_id") or "") for item in selected}
        for item in salient_page["items"]:
            if not (item.get("retrieval") or {}).get("matched_terms"):
                continue
            episode_id = str(item.get("episode_id") or "")
            if episode_id in seen:
                continue
            selected.append(item)
            seen.add(episode_id)
            if len(selected) >= safe_limit:
                break
        for item in recent:
            episode_id = str(item.get("episode_id") or "")
            if episode_id in seen:
                continue
            selected.append(item)
            seen.add(episode_id)
            if len(selected) >= safe_limit:
                break
        return selected[:safe_limit]

    def list_experience(
        self,
        research_id: str,
        *,
        query: str = "",
        limit: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        self._project(research_id)
        safe_limit = self._page_limit(limit)
        cursor_data = _cursor_decode(cursor, "experience")
        query_hash = sha256_text(_clean_text(query, 1000))
        if cursor_data and str(cursor_data.get("query_hash") or "") != query_hash:
            raise ValueError("research experience cursor does not match query")
        persistent = ResearchExperienceStore.list_items(
            **self.owner,
            research_id=research_id,
            include_history=False,
            limit=5000,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if persistent:
            query_terms = _terms(query)
            items: list[dict[str, Any]] = []
            for stored in persistent:
                content = _clean_text(stored.summary or stored.title, 12000)
                matched = query_terms & _terms(
                    " ".join([stored.title, stored.summary, stored.kind])
                )
                if query_terms and not matched:
                    continue
                item = stored.to_dict()
                item.update(
                    {
                        "research_id": (
                            stored.source_research_ids[0]
                            if len(stored.source_research_ids) == 1
                            else ""
                        ),
                        "type": stored.kind,
                        "content": content,
                        "source_episode_id": (
                            stored.source_episode_ids[0]
                            if stored.source_episode_ids
                            else ""
                        ),
                        "source_started_at": stored.occurred_at,
                        "source": {
                            "provenance": stored.provenance,
                            "activity_ids": stored.source_activity_ids,
                            "event_ids": stored.source_event_ids,
                        },
                        "retrieval": {
                            "matched_terms": sorted(
                                matched,
                                key=lambda value: (-len(value), value),
                            )[:20],
                            "score": round(
                                len(matched) * 2.0
                                + (1.0 if stored.durability == "durable" else 0.25),
                                4,
                            ),
                        },
                    }
                )
                items.append(item)
            items.sort(
                key=lambda item: (
                    float((item.get("retrieval") or {}).get("score") or 0),
                    str(item.get("occurred_at") or ""),
                    str(item.get("experience_id") or ""),
                ),
                reverse=True,
            )
            offset = max(0, int(cursor_data.get("offset") or 0))
            selected = items[offset : offset + safe_limit]
            next_cursor = ""
            if offset + safe_limit < len(items):
                next_cursor = _cursor_encode(
                    "experience",
                    {
                        "query_hash": query_hash,
                        "storage": "persistent",
                        "offset": offset + safe_limit,
                    },
                )
            return {
                "object_type": "experience",
                "query": _clean_text(query, 1000),
                "loaded_count": len(selected),
                "complete": not cursor and not next_cursor,
                "page_complete": not next_cursor,
                "next_cursor": next_cursor,
                "storage": "persistent",
                "items": selected,
            }
        episode_cursor = str(cursor_data.get("episode_cursor") or "")
        offset = max(0, int(cursor_data.get("offset") or 0))
        episode_page = self.list_episodes(
            research_id,
            query=query,
            limit=50,
            cursor=episode_cursor,
        )
        query_terms = _terms(query)
        items: list[dict[str, Any]] = []
        episode_by_id = {
            str(episode.get("episode_id") or ""): episode
            for episode in episode_page["items"]
        }
        for item in derive_experience_items(research_id, episode_page["items"]):
            matched = query_terms & _terms(item["content"])
            if query_terms and not matched:
                continue
            episode = episode_by_id.get(str(item.get("source_episode_id") or ""), {})
            item["retrieval"] = {
                "matched_terms": sorted(matched, key=lambda value: (-len(value), value))[:20],
                "score": round(
                    len(matched) * 2.0
                    + float((episode.get("retrieval") or {}).get("importance_score") or 0),
                    4,
                ),
            }
            items.append(item)
        items.sort(
            key=lambda item: (
                float((item.get("retrieval") or {}).get("score") or 0),
                item.get("source_started_at", ""),
            ),
            reverse=True,
        )
        selected = items[offset : offset + safe_limit]
        next_cursor = ""
        if offset + safe_limit < len(items):
            next_cursor = _cursor_encode(
                "experience",
                {
                    "query_hash": query_hash,
                    "episode_cursor": episode_cursor,
                    "offset": offset + safe_limit,
                },
            )
        elif episode_page.get("next_cursor"):
            next_cursor = _cursor_encode(
                "experience",
                {
                    "query_hash": query_hash,
                    "episode_cursor": str(episode_page["next_cursor"]),
                    "offset": 0,
                },
            )
        return {
            "object_type": "experience",
            "query": _clean_text(query, 1000),
            "loaded_count": len(selected),
            "complete": not cursor and not next_cursor,
            "page_complete": not next_cursor,
            "next_cursor": next_cursor,
            "items": selected,
        }

    def checkpoint_quality(self, research_id: str, checkpoint: Any | None = None) -> dict[str, Any]:
        self._project(research_id)
        item = checkpoint or ResearchDerivationStore.get_checkpoint(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if item is None:
            return {"status": "blocked", "score": 0.0, "issues": [{"severity": "error", "code": "checkpoint_missing"}]}
        issues: list[dict[str, Any]] = []
        score = 1.0

        def add(severity: str, code: str, message: str, penalty: float) -> None:
            nonlocal score
            issues.append({"severity": severity, "code": code, "message": message})
            score = max(0.0, score - penalty)

        if item.state != "active":
            add("error", "checkpoint_not_active", "Checkpoint 不是 active 状态。", 1.0)
        if item.budget_status in {"minimum_overflow", "conflict_overflow"}:
            add("error", "checkpoint_budget_overflow", "Checkpoint 的最小安全内容超过预算。", 0.7)
        if not _clean_text(item.current_state, 2000):
            add("error", "checkpoint_current_state_missing", "Checkpoint 缺少当前状态。", 0.5)
        if not _clean_text(item.research_goal, 1000):
            add("warning", "checkpoint_goal_missing", "Checkpoint 缺少研究目标。", 0.15)
        if not item.next_actions:
            add("warning", "checkpoint_next_actions_missing", "Checkpoint 没有可执行的下一步。", 0.08)

        conflict_page = self.list_conflicts(
            research_id,
            limit=self.MAX_MANDATORY_CONFLICTS,
        )
        checkpoint_conflicts = {
            str(value.get("conflict_id") or "")
            for value in item.known_conflicts
            if isinstance(value, dict) and str(value.get("conflict_id") or "")
        }
        current_conflicts = {
            str(value.get("conflict_id") or "")
            for value in conflict_page["items"]
        }
        if not conflict_page["complete"]:
            add("error", "checkpoint_conflicts_unbounded", "开放 Conflict 超过单次完整读取上限。", 0.7)
        elif checkpoint_conflicts != current_conflicts:
            add("warning", "checkpoint_conflicts_stale", "Checkpoint 中的 Conflict 集合不是当前完整集合。", 0.25)

        evidence_ids = {
            str(evidence_id or "")
            for fact in item.confirmed_facts
            if isinstance(fact, dict)
            for evidence_id in list(fact.get("evidence_ids") or [])
            if str(evidence_id or "") and not str(evidence_id).startswith("event:")
        }
        evidence = EvidenceStore.get_many(evidence_ids, workdir=self.workdir.as_posix())
        missing = sorted(evidence_ids - set(evidence))
        inactive = sorted(
            evidence_id
            for evidence_id, record in evidence.items()
            if str(record.status or "") != "active"
        )
        if missing:
            add("warning", "checkpoint_evidence_missing", "部分确认事实的 Evidence 已不存在。", 0.2)
        if inactive:
            add("warning", "checkpoint_evidence_inactive", "部分确认事实的 Evidence 需要复核。", 0.12)

        generated_at = str(item.generated_at or "")
        connection = self._connect()
        try:
            rows = [
                connection.execute(
                    "SELECT MAX(updated_at) FROM research_findings WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?",
                    [*self.owner.values(), research_id],
                ).fetchone()[0],
                connection.execute(
                    "SELECT MAX(updated_at) FROM research_conflicts WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=?",
                    [*self.owner.values(), research_id],
                ).fetchone()[0],
                connection.execute(
                    "SELECT MAX(started_at) FROM research_episodes WHERE tenant_id=? AND user_id=? AND workspace_id=? AND research_id=? AND state='active'",
                    [*self.owner.values(), research_id],
                ).fetchone()[0],
            ]
        finally:
            connection.close()
        latest_source_time = max((str(value or "") for value in rows), default="")
        if generated_at and latest_source_time and latest_source_time > generated_at:
            add("warning", "checkpoint_source_changed", "Checkpoint 生成后研究对象又发生了变化。", 0.18)

        status = "blocked" if any(value["severity"] == "error" for value in issues) else "warning" if issues else "ready"
        return {
            "status": status,
            "score": round(score, 4),
            "checkpoint_id": item.checkpoint_id,
            "checkpoint_revision": item.revision,
            "generated_at": item.generated_at,
            "issues": issues,
            "coverage": {
                "open_conflicts": int(conflict_page["total_count"]),
                "conflicts_complete": bool(conflict_page["complete"]),
                "confirmed_evidence": len(evidence_ids),
                "missing_evidence": len(missing),
                "inactive_evidence": len(inactive),
                "source_episodes": len(item.source_episode_ids),
            },
        }

    def list_pack_assets(
        self,
        *,
        query: str = "",
        limit: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        """List imported ResearchPack assets without loading their object payloads."""

        safe_limit = self._page_limit(limit)
        text = _clean_text(query, 1000)
        query_hash = sha256_text(text)
        cursor_data = _cursor_decode(cursor, "pack_asset")
        if cursor_data and str(cursor_data.get("query_hash") or "") != query_hash:
            raise ValueError("ResearchPack asset cursor does not match query")
        query_terms = _terms(text)
        assets = ResearchPackAssetStore.list_assets(
            **self.owner,
            status="active",
            limit=1000,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        items: list[dict[str, Any]] = []
        for asset in assets:
            searchable = " ".join(
                [
                    asset.title,
                    str(asset.summary.get("summary") or ""),
                    canonical_json(asset.manifest.get("researches") or []),
                ]
            )
            matched = query_terms & _terms(searchable)
            if query_terms and not matched:
                continue
            gate = dict(asset.summary.get("conflict_gate") or {})
            availability = dict(asset.summary.get("lineage_availability") or {})
            overlay = ResearchPackAssetStore.list_overlays(
                asset.asset_id,
                **self.owner,
                status="active",
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            items.append(
                {
                    "asset_id": asset.asset_id,
                    "pack_id": asset.pack_id,
                    "title": asset.title,
                    "scope_mode": asset.scope_mode,
                    "started_at": asset.started_at,
                    "ended_at": asset.ended_at,
                    "source_research_ids": asset.source_research_ids,
                    "generation": int(asset.lineage.get("generation") or 1),
                    "parent_pack_ids": list(asset.lineage.get("parent_pack_ids") or []),
                    "overlay_status": overlay[0].status if overlay else "missing",
                    "overlay_updated_at": overlay[0].updated_at if overlay else "",
                    "continuation_blocked": bool(gate.get("continuation_blocked")),
                    "continuation_warning": bool(availability.get("continuation_warning")),
                    "matched_terms": sorted(matched, key=lambda value: (-len(value), value))[:20],
                }
            )
        offset = max(0, int(cursor_data.get("offset") or 0))
        selected = items[offset : offset + safe_limit]
        next_cursor = ""
        if offset + safe_limit < len(items):
            next_cursor = _cursor_encode(
                "pack_asset",
                {"query_hash": query_hash, "offset": offset + safe_limit},
            )
        return {
            "schema": "tcode.research-read.pack-assets.v1",
            "query": text,
            "total_count": len(items),
            "loaded_count": len(selected),
            "page_complete": not next_cursor,
            "next_cursor": next_cursor,
            "items": selected,
            "content_trust": "untrusted_data",
            "automatic_execution_allowed": False,
        }

    def get_pack_overview(self, asset_id: str) -> dict[str, Any]:
        asset = ResearchPackAssetStore.get_asset(
            asset_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if asset is None or asset.asset_id != str(asset_id or "").strip():
            raise ValueError("ResearchPack asset not found")
        scopes = ResearchPackAssetStore.list_scopes(
            asset.asset_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        overlays = ResearchPackAssetStore.list_overlays(
            asset.asset_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        gate = dict(asset.summary.get("conflict_gate") or {})
        availability = dict(asset.summary.get("lineage_availability") or {})
        return {
            "schema": "tcode.research-read.pack-overview.v1",
            "asset": {
                "asset_id": asset.asset_id,
                "pack_id": asset.pack_id,
                "title": asset.title,
                "scope_mode": asset.scope_mode,
                "started_at": asset.started_at,
                "ended_at": asset.ended_at,
                "source_research_ids": asset.source_research_ids,
                "summary": asset.summary,
                "lineage": asset.lineage,
                "source_digest": asset.source_digest,
            },
            "scopes": [item.to_dict() for item in scopes],
            "overlays": [item.to_dict() for item in overlays],
            "parent_object_counts": ResearchPackAssetStore.object_counts(
                asset.asset_id,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            ),
            "overlay_object_counts": ResearchPackAssetStore.overlay_object_counts(
                asset.asset_id,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            ),
            "conflict_gate": {
                "status": "blocked" if gate.get("continuation_blocked") else "clear",
                **gate,
            },
            "lineage_availability": availability,
            "document_index": self._pack_document_index(asset.asset_id).state(),
            "read_contract": {
                "levels": ["overview", "scope", "search", "object"],
                "pagination": "opaque_cursor",
                "search_unit": "document_chunk",
                "search_returns_source_spans": True,
                "parent_objects_are_immutable": True,
                "overlay_objects_are_append_only": True,
                "content_trust": "untrusted_data",
                "automatic_execution_allowed": False,
            },
        }

    def list_pack_scopes(self, asset_id: str) -> dict[str, Any]:
        overview = self.get_pack_overview(asset_id)
        return {
            "schema": "tcode.research-read.pack-scopes.v1",
            "asset_id": asset_id,
            "count": len(overview["scopes"]),
            "items": overview["scopes"],
            "conflict_gate": overview["conflict_gate"],
        }

    def list_pack_objects(
        self,
        asset_id: str,
        *,
        object_type: str = "",
        source_research_id: str = "",
        query: str = "",
        limit: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        overview = self.get_pack_overview(asset_id)
        safe_limit = self._page_limit(limit)
        kind = str(object_type or "").strip().lower()
        source_id = str(source_research_id or "").strip()
        text = _clean_text(query, 1000)
        cursor_kind = "pack_object"
        cursor_data = _cursor_decode(cursor, cursor_kind)
        request_hash = sha256_text(
            canonical_json(
                {
                    "asset_id": asset_id,
                    "object_type": kind,
                    "source_research_id": source_id,
                    "query": text,
                }
            )
        )
        if cursor_data and str(cursor_data.get("request_hash") or "") != request_hash:
            raise ValueError("ResearchPack object cursor does not match request")
        query_terms = _terms(text)
        parent = ResearchPackAssetStore.list_objects(
            asset_id,
            **self.owner,
            object_type=kind,
            limit=10000,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        overlay = ResearchPackAssetStore.list_overlay_objects(
            asset_id,
            **self.owner,
            object_type=kind,
            limit=10000,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        items: list[dict[str, Any]] = []
        for item in parent:
            if source_id and source_id not in item.source_research_ids:
                continue
            matched = query_terms & _terms(canonical_json(item.payload))
            if query_terms and not matched:
                continue
            items.append(
                {
                    "layer": "parent",
                    "immutable": True,
                    "object_id": item.object_id,
                    "object_type": item.object_type,
                    "source_object_id": item.source_object_id,
                    "source_research_ids": item.source_research_ids,
                    "content_hash": item.content_hash,
                    "payload": item.payload,
                    "matched_terms": sorted(matched, key=lambda value: (-len(value), value))[:20],
                }
            )
        for item in overlay:
            ref_source_ids = {
                str(ref.get("source_research_id") or "")
                for ref in item.imported_scope_refs
            }
            if source_id and source_id not in ref_source_ids:
                continue
            matched = query_terms & _terms(canonical_json(item.payload))
            if query_terms and not matched:
                continue
            items.append(
                {
                    "layer": "overlay",
                    "immutable": False,
                    "object_id": item.overlay_object_id,
                    "object_type": item.object_type,
                    "local_object_id": item.local_object_id,
                    "revision": item.revision,
                    "imported_scope_refs": item.imported_scope_refs,
                    "origin_pack_ids": item.origin_pack_ids,
                    "content_hash": item.content_hash,
                    "payload": item.payload,
                    "matched_terms": sorted(matched, key=lambda value: (-len(value), value))[:20],
                }
            )
        items.sort(
            key=lambda item: (
                -len(item.get("matched_terms") or []),
                str(item.get("object_type") or ""),
                str(item.get("source_object_id") or item.get("local_object_id") or ""),
            )
        )
        offset = max(0, int(cursor_data.get("offset") or 0))
        selected = items[offset : offset + safe_limit]
        next_cursor = ""
        if offset + safe_limit < len(items):
            next_cursor = _cursor_encode(
                cursor_kind,
                {"request_hash": request_hash, "offset": offset + safe_limit},
            )
        return {
            "schema": "tcode.research-read.pack-objects.v1",
            "asset_id": asset_id,
            "object_type": kind,
            "source_research_id": source_id,
            "query": text,
            "total_count": len(items),
            "loaded_count": len(selected),
            "page_complete": not next_cursor,
            "next_cursor": next_cursor,
            "items": selected,
            "conflict_gate": overview["conflict_gate"],
            "content_trust": "untrusted_data",
            "automatic_execution_allowed": False,
        }

    def _pack_document_index(self, asset_id: str) -> ResearchPackDocumentIndex:
        return ResearchPackDocumentIndex(
            asset_id=asset_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )

    def get_pack_index_status(self, asset_id: str) -> dict[str, Any]:
        self.get_pack_overview(asset_id)
        return {
            "schema": "tcode.research-read.pack-index.v1",
            "asset_id": asset_id,
            "index": self._pack_document_index(asset_id).state(),
        }

    def rebuild_pack_index(
        self,
        asset_id: str,
        *,
        force_embeddings: bool = False,
    ) -> dict[str, Any]:
        self.get_pack_overview(asset_id)
        return {
            "schema": "tcode.research-read.pack-index.v1",
            "asset_id": asset_id,
            "index": self._pack_document_index(asset_id).rebuild(
                force_embeddings=force_embeddings,
            ),
        }

    def get_pack_object(
        self,
        asset_id: str,
        object_type: str,
        object_id: str,
    ) -> dict[str, Any]:
        self.get_pack_overview(asset_id)
        kind = str(object_type or "").strip().lower()
        identifier = str(object_id or "").strip()
        if not kind or not identifier:
            raise ValueError("object_type and object_id are required")
        parent = ResearchPackAssetStore.get_object(
            asset_id,
            identifier,
            **self.owner,
            object_type=kind,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if parent is not None:
            return {
                "schema": "tcode.research-read.pack-object.v1",
                "asset_id": asset_id,
                "layer": "parent",
                "immutable": True,
                "item": parent.to_dict(),
                "content_trust": "untrusted_data",
                "automatic_execution_allowed": False,
            }
        local = ResearchPackAssetStore.get_overlay_object(
            asset_id,
            identifier,
            **self.owner,
            object_type=kind,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if local is None:
            raise ValueError("ResearchPack object not found")
        return {
            "schema": "tcode.research-read.pack-object.v1",
            "asset_id": asset_id,
            "layer": "overlay",
            "immutable": False,
            "item": local.to_dict(),
            "content_trust": "untrusted_data",
            "automatic_execution_allowed": False,
        }

    def search_pack(
        self,
        asset_id: str,
        query: str,
        *,
        source_research_id: str = "",
        limit: int = 20,
        ready_only: bool = False,
    ) -> dict[str, Any]:
        text = _clean_text(query, 4000)
        if not text:
            raise ValueError("query is required")
        self.get_pack_overview(asset_id)
        index = self._pack_document_index(asset_id)
        search_method = index.search_available if ready_only else index.search
        result = search_method(
            text,
            source_research_id=source_research_id,
            limit=min(100, max(1, int(limit or 20))),
        )
        return {
            **result,
            "query_hash": sha256_text(text),
            "query_stored": False,
        }

    def get_brief(self, research_id: str) -> dict[str, Any]:
        project = self._project(research_id)
        checkpoint = ResearchDerivationStore.get_checkpoint(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        counts = self.counts(research_id)
        quality = self.checkpoint_quality(research_id, checkpoint)
        return {
            "schema": "tcode.research-read.brief.v1",
            "research": {
                "research_id": project.research_id,
                "title": project.title,
                "description": _clean_text(project.description, 1000),
                "status": project.status,
            },
            "checkpoint": {
                "checkpoint_id": checkpoint.checkpoint_id if checkpoint else "",
                "revision": checkpoint.revision if checkpoint else 0,
                "goal": _clean_text(checkpoint.research_goal, 500) if checkpoint else "",
                "current_state": _clean_text(checkpoint.current_state, 800) if checkpoint else "",
            },
            "checkpoint_quality": quality,
            "counts": counts,
            "read_contract": {
                "levels": ["brief", "handoff", "object", "artifact"],
                "default_level": "brief",
                "list_object_types": ["finding", "conflict", "episode", "experience"],
                "exact_object_types": [
                    "finding",
                    "conflict",
                    "episode",
                    "experience",
                    "checkpoint",
                    "evidence",
                    "paper",
                    "wiki_page",
                ],
                "pagination": "opaque_cursor",
                "object_reads_are_owner_scoped": True,
                "content_trust": "untrusted_data",
                "automatic_execution_allowed": False,
            },
        }

    def get_handoff(self, research_id: str, *, question: str = "") -> dict[str, Any]:
        brief = self.get_brief(research_id)
        checkpoint = ResearchDerivationStore.get_checkpoint(
            research_id,
            **self.owner,
            workdir=self.workdir.as_posix(),
            settings=self.settings,
        )
        if checkpoint is None:
            return {
                "schema": "tcode.research-read.handoff.v1",
                "status": "blocked",
                "reason": "checkpoint_missing",
                "brief": brief,
            }
        conflicts = self.list_conflicts(research_id, limit=self.MAX_MANDATORY_CONFLICTS)
        findings = self.list_findings(
            research_id,
            query=question,
            statuses=["active", "context_limited", "disputed"],
            limit=8,
        )
        episodes = self.select_episodes(research_id, question, limit=4)
        experience = self.list_experience(research_id, query=question, limit=8)
        quality = brief["checkpoint_quality"]
        status = "blocked" if quality["status"] == "blocked" or not conflicts["complete"] else "warning" if quality["status"] == "warning" else "ready"
        data = {
            "schema": "tcode.research-read.handoff.v1",
            "status": status,
            "research": brief["research"],
            "checkpoint": {
                **brief["checkpoint"],
                "open_questions": checkpoint.open_questions[:20],
                "next_actions": checkpoint.next_actions[:20],
                "important_artifacts": checkpoint.important_artifacts[:30],
            },
            "checkpoint_quality": quality,
            "conflict_gate": {
                "status": "required" if conflicts["total_count"] else "clear",
                "complete": conflicts["complete"],
                "total_count": conflicts["total_count"],
                "items": conflicts["items"],
            },
            "findings": findings["items"],
            "episodes": episodes,
            "experience": experience["items"],
            "content_trust": "untrusted_data",
            "automatic_execution_allowed": False,
            "retrieval": {
                "question_hash": sha256_text(_clean_text(question, 20000)) if question else "",
                "question_stored": False,
                "finding_selection": "query_relevance_then_recency",
                "episode_selection": "recent_plus_salient",
            },
        }
        data["source_digest"] = sha256_text(canonical_json(data))
        data["token_estimate"] = _estimate_tokens(data)
        return data

    def get_object(self, research_id: str, object_type: str, object_id: str) -> dict[str, Any]:
        self._project(research_id)
        kind = str(object_type or "").strip().lower()
        identifier = str(object_id or "").strip()
        if not identifier:
            raise ValueError("object_id is required")
        if kind == "finding":
            item = ResearchDerivationStore.get_finding(
                identifier,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            if item is None or item.research_id != research_id:
                raise ValueError("finding not found")
            return {
                "object_type": kind,
                "item": item.to_dict(),
                "versions": ResearchDerivationStore.list_finding_versions(
                    identifier,
                    **self.owner,
                    workdir=self.workdir.as_posix(),
                    settings=self.settings,
                ),
            }
        if kind == "conflict":
            item = ResearchDerivationStore.get_conflict(
                identifier,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            if item is None or item.research_id != research_id:
                raise ValueError("conflict not found")
            return {
                "object_type": kind,
                "item": item.to_dict(),
                "resolutions": [
                    resolution.to_dict()
                    for resolution in ResearchDerivationStore.list_resolutions(
                        identifier,
                        **self.owner,
                        workdir=self.workdir.as_posix(),
                        settings=self.settings,
                    )
                ],
            }
        if kind == "episode":
            item = ResearchDerivationStore.get_episode(
                identifier,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            if item is None or item.research_id != research_id:
                raise ValueError("episode not found")
            return {"object_type": kind, "item": item.to_dict()}
        if kind == "experience":
            persistent = ResearchExperienceStore.get(
                identifier,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            if (
                persistent is not None
                and research_id in persistent.source_research_ids
                and persistent.state == "active"
                and not persistent.superseded_by
            ):
                return {
                    "object_type": kind,
                    "item": persistent.to_dict(),
                    "layer": "persistent",
                }
            episode_cursor = ""
            while True:
                page = self.list_episodes(
                    research_id,
                    limit=self.MAX_PAGE_SIZE,
                    cursor=episode_cursor,
                )
                for item in derive_experience_items(research_id, page["items"]):
                    if item["experience_id"] == identifier:
                        return {
                            "object_type": kind,
                            "item": item,
                            "layer": "derived_from_episode",
                        }
                episode_cursor = str(page.get("next_cursor") or "")
                if not episode_cursor:
                    break
            raise ValueError("experience not found")
        if kind == "checkpoint":
            item = ResearchDerivationStore.get_checkpoint(
                research_id,
                **self.owner,
                workdir=self.workdir.as_posix(),
                settings=self.settings,
            )
            if item is None or item.checkpoint_id != identifier:
                raise ValueError("checkpoint not found")
            return {
                "object_type": kind,
                "item": item.to_dict(),
                "quality": self.checkpoint_quality(research_id, item),
            }
        if kind == "evidence":
            self._require_linked_object(research_id, kind, identifier)
            item = EvidenceStore.get(identifier, workdir=self.workdir.as_posix())
            if item is None:
                raise ValueError("evidence not found")
            return {"object_type": kind, "item": item.to_dict()}
        if kind == "paper":
            self._require_linked_object(research_id, kind, identifier)
            from ..server.paper_library_loader import paper_library_module

            module = paper_library_module()
            root = self.workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
            store = module.PaperLibraryStore(library_root=root, workdir=self.workdir.as_posix())
            record = store.get_record(identifier, sync_evidence=False)
            available_layers = list(record.get("available_layers") or [])
            if not available_layers:
                candidates = (
                    ("metadata", bool(record.get("metadata"))),
                    ("l0_summary", bool(record.get("l0_summary"))),
                    ("passages", bool(record.get("l1_passage_summaries"))),
                    ("facts", bool(record.get("l2_facts"))),
                    ("chunks", bool(record.get("l2_chunks"))),
                    ("anchors", bool(record.get("evidence_anchors"))),
                    ("materialized_views", bool(record.get("materialized_views"))),
                    ("visual_artifacts", bool(record.get("visual_artifacts"))),
                    ("doc_chunk_index", bool(record.get("doc_chunk_index"))),
                )
                available_layers = [name for name, available in candidates if available]
            return {
                "object_type": kind,
                "item": record,
                "available_layers": available_layers,
                "missing_layers": list(record.get("missing_layers") or []),
                "degraded_reason": str(record.get("degraded_reason") or ""),
            }
        if kind == "wiki_page":
            self._require_linked_object(research_id, kind, identifier)
            from ..server.paper_wiki_loader import paper_wiki_module

            module = paper_wiki_module()
            root = self.workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
            store = module.PaperWikiStore(
                library_root=root,
                workdir=self.workdir.as_posix(),
                vector_search_enabled=True,
            )
            page = store.get_page(identifier)
            if not page:
                raise ValueError("wiki_page not found")
            return {"object_type": kind, "item": page}
        raise ValueError(f"unsupported research object type: {kind}")

    def search(self, research_id: str, query: str, *, limit: int = 8) -> dict[str, Any]:
        self._project(research_id)
        text = _clean_text(query, 4000)
        if not text:
            raise ValueError("query is required")
        safe_limit = max(1, min(int(limit or 8), 50))
        findings = self.list_findings(research_id, query=text, limit=safe_limit)
        conflicts = self.list_conflicts(
            research_id,
            statuses=["open", "under_review", "resolved"],
            query=text,
            limit=safe_limit,
        )
        episodes = self.list_episodes(research_id, query=text, limit=safe_limit)
        experience = self.list_experience(research_id, query=text, limit=safe_limit)
        evidence_scope = self._linked_object_scope(research_id, "evidence")
        # Keep one SQLite IN clause below conservative host-variable limits.
        wiki_scope = self._linked_object_scope(research_id, "wiki_page", limit=800)
        paper_scope = self._linked_object_scope(research_id, "paper")
        evidence_records = EvidenceStore.search(
            query=text,
            workdir=self.workdir.as_posix(),
            limit=safe_limit,
            include_inactive=True,
            allowed_ids=evidence_scope["ids"],
        )
        wiki: dict[str, Any] = {
            "status": "unavailable",
            "hits": [],
            "search_mode": "unavailable",
        }
        papers: dict[str, Any] = {"status": "unavailable", "hits": []}
        library_root = self.workdir / ".tcode" / "plugins" / "paper_research_workflow" / "library"
        try:
            from ..server.paper_wiki_loader import paper_wiki_module

            if (library_root / "wiki" / "state" / "wiki.sqlite3").is_file():
                wiki_module = paper_wiki_module()
                wiki_store = wiki_module.PaperWikiStore(
                    library_root=library_root,
                    workdir=self.workdir.as_posix(),
                    vector_search_enabled=True,
                )
                wiki = {
                    "status": "ready",
                    **wiki_store.search(
                        text,
                        limit=safe_limit,
                        allowed_page_ids=wiki_scope["ids"],
                    ),
                }
        except Exception as exc:
            wiki["error_type"] = type(exc).__name__
        try:
            from ..server.paper_library_loader import paper_library_module

            paper_module = paper_library_module()
            paper_store = paper_module.PaperLibraryStore(
                library_root=library_root,
                workdir=self.workdir.as_posix(),
            )
            papers = {
                "status": "ready",
                **paper_store.query(
                    text,
                    limit_docs=min(5, safe_limit),
                    limit_evidence=safe_limit,
                    sync_evidence=False,
                    allowed_doc_ids=paper_scope["ids"],
                ),
            }
        except Exception as exc:
            papers["error_type"] = type(exc).__name__
        return {
            "schema": "tcode.research-read.search.v1",
            "query_hash": sha256_text(text),
            "query_stored": False,
            "findings": findings,
            "conflicts": conflicts,
            "episodes": episodes,
            "experience": experience,
            "evidence": [item.to_dict() for item in evidence_records],
            "wiki": wiki,
            "papers": papers,
            "scope": {
                "evidence": {key: value for key, value in evidence_scope.items() if key != "ids"},
                "wiki_page": {key: value for key, value in wiki_scope.items() if key != "ids"},
                "paper": {key: value for key, value in paper_scope.items() if key != "ids"},
                "complete": all(
                    scope["complete"]
                    for scope in (evidence_scope, wiki_scope, paper_scope)
                ),
            },
        }


__all__ = ["ResearchReadService", "derive_experience_items"]
