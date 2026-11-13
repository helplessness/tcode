"""Explicit task state graph for complex multi-step work."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class TaskNode:
    id: str
    title: str
    owner: str
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    next_action: str = ""


@dataclass
class TaskGraph:
    task_id: str
    goal: str
    status: str = "pending"
    subtasks: list[TaskNode] = field(default_factory=list)
    current_blockers: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now().timestamp())


class TaskStateGraphManager:
    _STATUS_COLORS = {
        "pending": ("#30363d", "#c9d1d9"),
        "running": ("#0f4c5c", "#d6f5ff"),
        "completed": ("#1f6f43", "#eafff1"),
        "partial": ("#6b5413", "#fff7d6"),
        "blocked": ("#6b3f13", "#fff0d6"),
        "failed": ("#7a2f35", "#fff0f0"),
        "cancelled": ("#4b4650", "#f0ebf4"),
    }

    @staticmethod
    def _escape_label(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', "\\\"").replace("\n", " ")

    @classmethod
    def to_mermaid(cls, task_id: str) -> str:
        graph = cls.get(task_id)
        if not graph:
            return ""
        lines = ["flowchart TD", f'  root["{cls._escape_label(graph.goal or graph.task_id)}"]']
        node_ids = {node.id: f"n{index}" for index, node in enumerate(graph.subtasks, start=1)}
        for node in graph.subtasks:
            node_ref = node_ids[node.id]
            label = cls._escape_label(f"{node.title} [{node.status}]")
            lines.append(f'  {node_ref}["{label}"]')
            if node.depends_on:
                for dependency in node.depends_on:
                    source_ref = node_ids.get(dependency)
                    if source_ref:
                        lines.append(f"  {source_ref} --> {node_ref}")
            else:
                lines.append(f"  root --> {node_ref}")
            lines.append(f"  class {node_ref} status_{re.sub(r'[^a-z0-9_]', '_', node.status.lower())}")
        for status, (fill, color) in cls._STATUS_COLORS.items():
            lines.append(f"  classDef status_{status} fill:{fill},color:{color},stroke:#59636b")
        return "\n".join(lines)

    @classmethod
    def to_dot(cls, task_id: str) -> str:
        graph = cls.get(task_id)
        if not graph:
            return ""
        lines = ["digraph task_graph {", "  rankdir=TB;", f'  root [label="{cls._escape_label(graph.goal or graph.task_id)}"];']
        node_ids = {node.id: f"n{index}" for index, node in enumerate(graph.subtasks, start=1)}
        for node in graph.subtasks:
            node_ref = node_ids[node.id]
            label = cls._escape_label(f"{node.title} [{node.status}]")
            fill, color = cls._STATUS_COLORS.get(node.status, cls._STATUS_COLORS["pending"])
            lines.append(f'  {node_ref} [label="{label}", style=filled, fillcolor="{fill}", fontcolor="{color}"];')
            dependencies = [node_ids[item] for item in node.depends_on if item in node_ids]
            for source_ref in dependencies or ["root"]:
                lines.append(f"  {source_ref} -> {node_ref};")
        lines.append("}")
        return "\n".join(lines)
    @classmethod
    def _derive_graph_status(cls, graph: TaskGraph) -> str:
        if not graph.subtasks:
            return "pending"

        statuses = {node.status for node in graph.subtasks}
        if "failed" in statuses:
            return "failed"
        if "blocked" in statuses:
            return "blocked"
        if "partial" in statuses:
            return "partial"
        if "cancelled" in statuses:
            return "cancelled"
        if statuses and statuses <= {"completed"}:
            return "completed"
        if "running" in statuses:
            return "running"
        return "pending"

    @classmethod
    def _storage_path(cls) -> Path:
        base = os.environ.get("TCODE_WORKDIR", os.getcwd())
        return Path(base) / ".tcode" / "task_graphs.json"

    @classmethod
    def _load_all(cls) -> dict[str, TaskGraph]:
        path = cls._storage_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        graphs: dict[str, TaskGraph] = {}
        if isinstance(raw, dict):
            for task_id, value in raw.items():
                if not isinstance(value, dict):
                    continue
                subtasks = []
                for item in value.get("subtasks", []):
                    if isinstance(item, dict):
                        subtasks.append(TaskNode(**item))
                graphs[task_id] = TaskGraph(
                    task_id=str(value.get("task_id", task_id)),
                    goal=str(value.get("goal", "")),
                    status=str(value.get("status", "pending")),
                    subtasks=subtasks,
                    current_blockers=list(value.get("current_blockers", [])),
                    next_actions=list(value.get("next_actions", [])),
                    created_at=float(value.get("created_at", datetime.now().timestamp())),
                    updated_at=float(value.get("updated_at", datetime.now().timestamp())),
                )
        return graphs

    @classmethod
    def _save_all(cls, graphs: dict[str, TaskGraph]) -> None:
        path = cls._storage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            task_id: {
                **asdict(graph),
                "subtasks": [asdict(node) for node in graph.subtasks],
            }
            for task_id, graph in graphs.items()
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def ensure_graph(cls, task_id: str, goal: str) -> TaskGraph:
        graphs = cls._load_all()
        graph = graphs.get(task_id)
        if graph is None:
            graph = TaskGraph(task_id=task_id, goal=goal)
            graphs[task_id] = graph
            cls._save_all(graphs)
        return graph

    @classmethod
    def update_subtask(
        cls,
        task_id: str,
        *,
        subtask_id: str,
        title: str,
        owner: str,
        status: str,
        depends_on: Optional[list[str]] = None,
        evidence: Optional[list[str]] = None,
        next_action: str = "",
    ) -> TaskGraph:
        graphs = cls._load_all()
        graph = graphs.get(task_id) or TaskGraph(task_id=task_id, goal=title)
        existing = None
        for node in graph.subtasks:
            if node.id == subtask_id:
                existing = node
                break
        if existing is None:
            existing = TaskNode(id=subtask_id, title=title, owner=owner)
            graph.subtasks.append(existing)

        existing.title = title
        existing.owner = owner
        existing.status = status
        if depends_on is not None:
            existing.depends_on = list(depends_on)
        if evidence is not None:
            existing.evidence = list(evidence)
        if next_action:
            existing.next_action = next_action

        graph.status = cls._derive_graph_status(graph)

        graph.updated_at = datetime.now().timestamp()
        graphs[task_id] = graph
        cls._save_all(graphs)
        return graph

    @classmethod
    def set_next_actions(
        cls,
        task_id: str,
        next_actions: list[str],
        blockers: Optional[list[str]] = None,
    ) -> TaskGraph:
        graphs = cls._load_all()
        graph = graphs.get(task_id) or TaskGraph(task_id=task_id, goal="")
        graph.next_actions = [str(v).strip() for v in next_actions if str(v).strip()]
        if blockers is not None:
            graph.current_blockers = [str(v).strip() for v in blockers if str(v).strip()]
        graph.updated_at = datetime.now().timestamp()
        graphs[task_id] = graph
        cls._save_all(graphs)
        return graph

    @classmethod
    def sync_from_todos(
        cls,
        task_id: str,
        *,
        goal: str,
        todos: list[dict],
        owner: str = "planner",
    ) -> TaskGraph:
        graphs = cls._load_all()
        graph = graphs.get(task_id) or TaskGraph(task_id=task_id, goal=goal or "")

        if goal:
            graph.goal = goal

        next_actions: list[str] = []
        blockers: list[str] = []
        subtasks: list[TaskNode] = []

        status_map = {
            "pending": "pending",
            "in_progress": "running",
            "completed": "completed",
            "cancelled": "cancelled",
        }

        for index, raw in enumerate(todos or [], start=1):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("content", "")).strip()
            if not title:
                continue

            raw_status = str(raw.get("status", "pending")).strip().lower()
            status = status_map.get(raw_status, "pending")
            next_action = title if status in {"pending", "running"} else ""
            if next_action:
                next_actions.append(title)
            if status == "cancelled":
                blockers.append(f"Cancelled: {title}")

            subtasks.append(
                TaskNode(
                    id=f"todo-{index}",
                    title=title,
                    owner=owner or "planner",
                    status=status,
                    next_action=next_action,
                )
            )

        graph.subtasks = subtasks
        graph.current_blockers = blockers[:5]
        graph.next_actions = next_actions[:5]
        graph.status = cls._derive_graph_status(graph)
        graph.updated_at = datetime.now().timestamp()
        graphs[task_id] = graph
        cls._save_all(graphs)
        return graph

    @classmethod
    def get(cls, task_id: str) -> Optional[TaskGraph]:
        return cls._load_all().get(task_id)

    @classmethod
    def summarize(cls, task_id: str) -> dict:
        graph = cls.get(task_id)
        if not graph:
            return {}
        status_counts: dict[str, int] = {}
        for node in graph.subtasks:
            status_counts[node.status] = status_counts.get(node.status, 0) + 1
        return {
            "task_id": graph.task_id,
            "goal": graph.goal,
            "status": graph.status,
            "current_blockers": list(graph.current_blockers),
            "next_actions": list(graph.next_actions),
            "status_counts": status_counts,
            "mermaid": cls.to_mermaid(task_id),
            "dot": cls.to_dot(task_id),
            "subtasks": [
                {
                    "id": node.id,
                    "title": node.title,
                    "owner": node.owner,
                    "status": node.status,
                    "depends_on": list(node.depends_on),
                    "evidence": list(node.evidence),
                    "next_action": node.next_action,
                }
                for node in graph.subtasks
            ],
        }
