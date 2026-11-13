"""Hierarchical context router with recursive drill-down trace support."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from ..memory import MemoryManager
from ..memory.retriever import build_embedding, cosine_similarity, semantic_rank
from ..task.state_graph import TaskStateGraphManager
from ..team.memory import TeamMemoryManager
from ..team.registry import WorkerRegistry


TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass
class RetrievalTraceStep:
    phase: str
    selected: str
    reason: str
    count: int = 0
    metadata: Optional[dict[str, Any]] = None


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in TOKEN_RE.finditer(str(text or ""))}


def _score_overlap(query: str, text: str) -> float:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0.0
    target_tokens = _tokenize(text)
    if not target_tokens:
        return 0.0
    token_score = len(query_tokens & target_tokens) / max(1, len(query_tokens))
    query_text = str(query or "").strip().lower()
    target_text = str(text or "").strip().lower()
    contains_score = 0.0
    if query_text and target_text:
        if query_text in target_text or target_text in query_text:
            contains_score = 1.0
    return max(token_score, contains_score)


def _dedupe_by_id(items: list[Any], limit: int) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(getattr(item, "id", "") or "")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _worker_scope_text(worker: Any) -> str:
    return " ".join(
        [
            str(getattr(worker, "name", "") or ""),
            str(getattr(worker, "role", "") or ""),
            " ".join(getattr(worker, "skill_tags", []) or []),
            " ".join(getattr(worker, "common_tools", []) or []),
            str(getattr(worker, "recent_failure_reason", "") or ""),
        ]
    ).strip()


def _task_scope_text(node: Any) -> str:
    return " ".join(
        [
            str(getattr(node, "title", "") or ""),
            str(getattr(node, "owner", "") or ""),
            str(getattr(node, "status", "") or ""),
            str(getattr(node, "next_action", "") or ""),
            " ".join(getattr(node, "depends_on", []) or []),
            " ".join(getattr(node, "evidence", []) or []),
        ]
    ).strip()


def _team_entry_text(entry: Any) -> str:
    return " ".join(
        [
            str(getattr(entry, "search_text", "") or ""),
            str(getattr(entry, "content", "") or ""),
            str(getattr(entry, "source", "") or ""),
            str(getattr(entry, "goal", "") or ""),
            str(getattr(entry, "decision", "") or ""),
            str(getattr(entry, "outcome", "") or ""),
        ]
    ).strip()


def _rank_items(query: str, items: list[Any], text_getter) -> list[Any]:
    return semantic_rank(
        query=query,
        items=items,
        text_getter=text_getter,
        embedding_getter=lambda item: getattr(item, "embedding", None),
    )


def _rank_scope_candidates(
    query: str,
    candidates: list[tuple[Any, str]],
    *,
    workdir: str,
) -> list[tuple[Any, float]]:
    scored: list[tuple[Any, float]] = []
    query_embedding = build_embedding(query, workdir=workdir)
    for item, text in candidates:
        overlap_score = _score_overlap(query, text)
        embedding_score = cosine_similarity(query_embedding, build_embedding(text, workdir=workdir))
        score = embedding_score * 0.75 + overlap_score * 0.25
        if score > 0:
            scored.append((item, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def _filter_entries_for_worker(entries: list[Any], worker_name: str) -> list[Any]:
    expected_source = f"worker:{str(worker_name or '').strip().lower()}"
    filtered: list[Any] = []
    for entry in entries:
        source = str(getattr(entry, "source", "") or "").strip().lower()
        if source == expected_source:
            filtered.append(entry)
    return filtered


def _filter_entries_for_task(entries: list[Any], node: Any) -> list[Any]:
    title = str(getattr(node, "title", "") or "").strip().lower()
    owner = str(getattr(node, "owner", "") or "").strip().lower()
    filtered: list[Any] = []
    for entry in entries:
        haystack = _team_entry_text(entry).lower()
        source = str(getattr(entry, "source", "") or "").strip().lower()
        if title and title in haystack:
            filtered.append(entry)
            continue
        if owner and source == f"worker:{owner}":
            filtered.append(entry)
            continue
    return filtered


def route_context(
    *,
    session_id: str,
    workdir: str,
    query: str,
    team_mode: bool,
    limit: int = 8,
) -> dict[str, Any]:
    trace: list[RetrievalTraceStep] = []
    query_text = str(query or "").strip()
    safe_limit = max(2, min(limit or 8, 20))

    global_items = MemoryManager.query_related(
        workdir=workdir,
        goal=query_text,
        query=query_text,
        limit=safe_limit,
    )
    trace.append(
        RetrievalTraceStep(
            phase="scope_select",
            selected="global_memory",
            reason="default semantic recall from global memory",
            count=len(global_items),
        )
    )

    team_items = []
    worker_profiles = []
    task_graph = None

    if team_mode:
        team_items = TeamMemoryManager.query_related(
            session_id,
            workdir,
            goal=query_text,
            query=query_text,
            limit=max(2, safe_limit // 2),
        )
        trace.append(
            RetrievalTraceStep(
                phase="scope_select",
                selected="team_memory",
                reason="manager/team session detected, start team-scoped semantic recall",
                count=len(team_items),
            )
        )

        workers = WorkerRegistry.list_for_team(session_id)
        worker_profiles = [worker for worker in workers if worker.status != "terminated"]
        ranked_workers = _rank_scope_candidates(
            query_text,
            [(worker, _worker_scope_text(worker)) for worker in worker_profiles],
            workdir=workdir,
        )
        selected_workers = [worker for worker, _ in ranked_workers[:2]]
        worker_scope_fallback = False
        if not selected_workers and worker_profiles:
            selected_workers = worker_profiles[:1]
            worker_scope_fallback = True
        trace.append(
            RetrievalTraceStep(
                phase="scope_rank",
                selected="worker_profiles",
                reason="rank worker subscopes by role, skills, tools, and failure history",
                count=len(selected_workers),
                metadata={
                    "fallback_used": worker_scope_fallback,
                    "selected_workers": [
                        {
                            "name": worker.name,
                            "role": worker.role,
                            "score": round(score, 3),
                        }
                        for worker, score in ranked_workers[:4]
                    ]
                },
            )
        )

        task_graph = TaskStateGraphManager.get(session_id)
        selected_tasks: list[Any] = []
        team_entries_all = TeamMemoryManager.load(session_id, workdir)
        if task_graph:
            ranked_tasks = _rank_scope_candidates(
                query_text,
                [(node, _task_scope_text(node)) for node in task_graph.subtasks],
                workdir=workdir,
            )
            selected_tasks = [node for node, _ in ranked_tasks[:2]]
            task_scope_fallback = False
            if not selected_tasks and task_graph.subtasks:
                selected_tasks = sorted(
                    task_graph.subtasks,
                    key=lambda node: 0 if node.status == "running" else 1,
                )[:1]
                task_scope_fallback = True
            trace.append(
                RetrievalTraceStep(
                    phase="scope_rank",
                    selected="task_graph",
                    reason="rank task subscopes by title, owner, status, and next action",
                    count=len(selected_tasks),
                    metadata={
                        "fallback_used": task_scope_fallback,
                        "selected_tasks": [
                            {
                                "id": node.id,
                                "owner": node.owner,
                                "status": node.status,
                                "score": round(score, 3),
                            }
                            for node, score in ranked_tasks[:4]
                        ]
                    },
                )
            )

        worker_drilldown_items: list[Any] = []
        for worker in selected_workers:
            scoped_entries = _filter_entries_for_worker(team_entries_all, worker.name)
            ranked_entries = _rank_items(
                f"{query_text}\nworker {worker.name} {worker.role}",
                scoped_entries,
                _team_entry_text,
            )
            picked = ranked_entries[:2]
            worker_drilldown_items.extend(picked)
            trace.append(
                RetrievalTraceStep(
                    phase="worker_drilldown",
                    selected=worker.name,
                    reason="run second-pass recall inside worker-scoped team memories",
                    count=len(picked),
                    metadata={
                        "scope": f"worker:{worker.name}",
                        "memory_ids": [getattr(item, "id", "") for item in picked],
                    },
                )
            )

        task_drilldown_items: list[Any] = []
        for node in selected_tasks:
            scoped_entries = _filter_entries_for_task(team_entries_all, node)
            ranked_entries = _rank_items(
                f"{query_text}\nsubtask {node.title} owner {node.owner}",
                scoped_entries,
                _team_entry_text,
            )
            picked = ranked_entries[:2]
            task_drilldown_items.extend(picked)
            trace.append(
                RetrievalTraceStep(
                    phase="task_drilldown",
                    selected=node.id,
                    reason="run second-pass recall inside task-scoped team memories",
                    count=len(picked),
                    metadata={
                        "task_title": node.title,
                        "owner": node.owner,
                        "memory_ids": [getattr(item, "id", "") for item in picked],
                    },
                )
            )

        # Aggregate: drill-down items (worker + task) get L2 for top-3 hits,
        # preserving the expensive recursive recall precision. Other items stay L1.
        all_items = _dedupe_by_id(
            [*team_items, *worker_drilldown_items, *task_drilldown_items],
            safe_limit,
        )
        drilldown_ids = {
            getattr(item, "id", "")
            for item in (*worker_drilldown_items, *task_drilldown_items)
            if getattr(item, "id", "")
        }
        team_items = []
        l2_budget = 3
        for item in all_items:
            item_id = getattr(item, "id", "")
            if item_id in drilldown_ids and l2_budget > 0:
                team_items.append(
                    TeamMemoryManager._apply_level([item], "l2")[0]
                )
                l2_budget -= 1
            else:
                team_items.append(
                    TeamMemoryManager._apply_level([item], "l1")[0]
                )
        trace.append(
            RetrievalTraceStep(
                phase="taskgraph_select",
                selected="task_graph" if task_graph else "none",
                reason="include current task graph for project state context and recursive retrieval",
                count=len(task_graph.subtasks) if task_graph else 0,
            )
        )

    final_selected = len(global_items) + len(team_items) + len(worker_profiles)
    if task_graph:
        final_selected += 1
    trace.append(
        RetrievalTraceStep(
            phase="aggregation",
            selected="final_context",
            reason="aggregate global memory, recursive team memory drill-down, worker profiles, and task graph",
            count=final_selected,
        )
    )

    return {
        "memory_recall": global_items,
        "team_memory_recall": team_items,
        "worker_profiles": worker_profiles,
        "task_graph": task_graph,
        "trace": [asdict(step) for step in trace],
    }
