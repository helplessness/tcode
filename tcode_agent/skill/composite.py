"""Composite Skill planning built on top of the existing single-skill router.

The planner is intentionally conservative: it only produces a visible plan when
multiple sub-tasks route to multiple distinct skills. Execution remains owned by
the Agent, Workflow, MCP, and Human Review layers.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .router import SkillRouter
from .routing_config import load_skill_routing_config
from .routing_profile import SkillRoutingProfile
from .trace import SkillRoutingTraceStore, query_fingerprint, utc_now_iso


_CLAUSE_SPLIT_RE = re.compile(
    r"[,，;；。.\n]+|\s*(?:然后|接着|随后|最后|再|以及|并且|->|→)\s*",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")

_ACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("literature", r"(?:查找|查询|检索|搜索|下载|阅读).{0,24}(?:论文|文献|pdf|pubmed|doi|arxiv)"),
    ("screening", r"(?:筛选|选择|挑选).{0,24}(?:候选|酶|蛋白|parent|靶点)"),
    ("mutation", r"(?:突变设计|设计突变|序列设计|mutation|mutant|定向进化)"),
    ("structure", r"(?:结构预测|预测结构|boltz|alphafold|蛋白结构预测)"),
    ("docking", r"(?:分子对接|docking|gnina|配体对接|对接)"),
    ("interaction", r"(?:相互作用|氢键|疏水|plip|interaction)"),
    ("report", r"(?:设计报告|决策报告|生成报告|出报告|总结报告|报告)"),
)


@dataclass
class CompositeSkillStep:
    step_id: str
    subtask: str
    route_decision: str
    selected_skill: str = ""
    skill_id: str = ""
    confidence: float = 0.0
    depends_on: list[str] = field(default_factory=list)
    execution_hint: str = "instructional"
    required_inputs: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    conflicts_with: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    route_request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "subtask": self.subtask,
            "route_decision": self.route_decision,
            "selected_skill": self.selected_skill or None,
            "skill_id": self.skill_id or None,
            "confidence": round(float(self.confidence or 0.0), 6),
            "depends_on": list(self.depends_on),
            "execution_hint": self.execution_hint,
            "required_inputs": list(self.required_inputs),
            "missing_inputs": list(self.missing_inputs),
            "conflicts_with": list(self.conflicts_with),
            "candidates": list(self.candidates),
            "route_request_id": self.route_request_id or None,
        }


@dataclass
class CompositeSkillPlan:
    decision: str = "no-composite"
    mode: str = "off"
    reason: str = ""
    steps: list[CompositeSkillStep] = field(default_factory=list)
    confidence: float = 0.0
    trace: dict[str, Any] = field(default_factory=dict)
    expose_to_agent: bool = False
    level3_candidate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "mode": self.mode,
            "reason": self.reason,
            "confidence": round(float(self.confidence or 0.0), 6),
            "expose_to_agent": bool(self.expose_to_agent),
            "level3_candidate": bool(self.level3_candidate),
            "steps": [step.to_dict() for step in self.steps],
            "trace": dict(self.trace),
        }


def _clean_clause(text: str) -> str:
    cleaned = _SPACE_RE.sub(" ", str(text or "").strip(" \t\r\n，,；;。." ))
    for prefix in ("帮我", "请帮我", "请", "麻烦", "需要"):
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix) + 1:
            cleaned = cleaned[len(prefix) :].strip()
    return cleaned


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _decompose_query(query: str, max_steps: int) -> list[str]:
    text = str(query or "").strip()
    if not text:
        return []
    clauses = [_clean_clause(part) for part in _CLAUSE_SPLIT_RE.split(text)]
    clauses = [part for part in clauses if len(part) >= 2]
    clauses = _dedupe(clauses)
    if len(clauses) >= 2:
        return clauses[:max_steps]

    extracted: list[str] = []
    lower = text.lower()
    for _, pattern in _ACTION_PATTERNS:
        match = re.search(pattern, lower, flags=re.IGNORECASE)
        if match:
            extracted.append(_clean_clause(match.group(0)))
    return _dedupe([item for item in extracted if len(item) >= 2])[:max_steps]


def _profile_by_name(skills: list[Any]) -> dict[str, SkillRoutingProfile]:
    profiles: dict[str, SkillRoutingProfile] = {}
    for skill in skills:
        try:
            profile = SkillRoutingProfile.from_skill(skill)
        except Exception:
            continue
        profiles[profile.skill.name] = profile
        if profile.skill.skill_id:
            profiles[profile.skill.skill_id] = profile
    return profiles


def _execution_hint(profile: SkillRoutingProfile | None) -> str:
    if profile is None:
        return "instructional"
    name = str(profile.skill.name or "").lower()
    role = str(profile.skill_role or "").lower()
    domains = {str(item).lower() for item in profile.domains}
    if role == "runner" or name.endswith("-runner"):
        return "async-domain-mcp"
    if "literature" in domains or "document" in domains:
        return "tool-assisted"
    return "instructional"


def _missing_inputs(subtask: str, required_inputs: list[str]) -> list[str]:
    lower = str(subtask or "").lower()
    missing: list[str] = []
    for item in required_inputs:
        key = str(item or "").strip()
        if not key:
            continue
        normalized = key.lower().replace("_", " ")
        if key.lower() in lower or normalized in lower:
            continue
        # File-like task snippets often omit formal field names; keep this as a
        # reminder instead of a hard validation failure.
        missing.append(key)
    return missing


class SkillCompositePlanner:
    @classmethod
    def plan(
        cls,
        *,
        query: str,
        skills: list[Any],
        agent_name: str = "",
        recall_texts: Optional[list[str]] = None,
        workdir: Optional[str] = None,
        catalog_snapshot: Optional[dict[str, Any]] = None,
        use_vector: bool = True,
    ) -> CompositeSkillPlan:
        config = load_skill_routing_config(workdir)
        composite_cfg = config.get("composite", {}) if isinstance(config.get("composite"), dict) else {}
        mode = str(composite_cfg.get("mode") or "off").strip().lower()
        if not bool(composite_cfg.get("enabled", False)) or mode in {"off", "disabled", "legacy"}:
            return CompositeSkillPlan(decision="no-composite", mode=mode or "off", reason="disabled")

        max_steps = max(2, min(12, int(composite_cfg.get("max_steps", 6) or 6)))
        min_steps = max(2, min(max_steps, int(composite_cfg.get("min_steps", 2) or 2)))
        min_distinct = max(2, min(max_steps, int(composite_cfg.get("min_distinct_skills", 2) or 2)))
        min_step_score = max(0.0, min(1.0, float(composite_cfg.get("min_step_score", 0.30) or 0.30)))
        include_unrouted = bool(composite_cfg.get("include_unrouted_steps", True))
        expose_to_agent = bool(composite_cfg.get("expose_to_agent", mode == "active")) and mode == "active"

        subtasks = _decompose_query(query, max_steps=max_steps)
        base_trace = {
            "event_type": "skill_composite_route",
            "request_id": str(uuid.uuid4()),
            "router_version": "p2-composite-v1",
            "mode": mode,
            "catalog_snapshot_id": str((catalog_snapshot or {}).get("snapshot_id") or "unknown"),
            "agent_name": str(agent_name or ""),
            "query_hash": query_fingerprint(query),
            "created_at": utc_now_iso(),
        }

        if len(subtasks) < min_steps:
            base_trace.update({"decision": "no-composite", "reason": "not-enough-subtasks", "subtasks": subtasks})
            SkillRoutingTraceStore.append_route_trace(base_trace, workdir=workdir, config=config)
            return CompositeSkillPlan(
                decision="no-composite",
                mode=mode,
                reason="not-enough-subtasks",
                trace=base_trace,
            )

        snapshot = catalog_snapshot or {"snapshot_id": "unknown", "skill_count": len(skills)}
        profiles = _profile_by_name(skills)
        steps: list[CompositeSkillStep] = []
        selected_skills: list[str] = []
        selected_scores: list[float] = []

        previous_step_id = ""
        for index, subtask in enumerate(subtasks, start=1):
            route_result = SkillRouter.route(
                query=subtask,
                skills=skills,
                agent_name=agent_name,
                recall_texts=recall_texts,
                limit=3,
                workdir=workdir,
                catalog_snapshot=snapshot,
                use_vector=use_vector,
            )
            selected = route_result.primary_skill if route_result.decision == "single" else ""
            confidence = float(route_result.confidence or 0.0)
            if selected and confidence < min_step_score:
                selected = ""
            profile = profiles.get(selected) if selected else None
            required_inputs = list(profile.required_inputs) if profile else []
            step_id = f"s{index}"
            candidates = [
                {
                    "name": item.get("name"),
                    "skill_id": item.get("skill_id"),
                    "score": item.get("score"),
                    "route_decision": route_result.decision,
                }
                for item in (route_result.candidates or [])[:3]
            ]
            step = CompositeSkillStep(
                step_id=step_id,
                subtask=subtask,
                route_decision=route_result.decision,
                selected_skill=selected,
                skill_id=str(getattr(profile.skill, "skill_id", "") or "") if profile else "",
                confidence=confidence,
                depends_on=[previous_step_id] if previous_step_id else [],
                execution_hint=_execution_hint(profile),
                required_inputs=required_inputs,
                missing_inputs=_missing_inputs(subtask, required_inputs) if selected else [],
                conflicts_with=list(profile.conflicts_with) if profile else [],
                candidates=candidates,
                route_request_id=str(route_result.trace.get("request_id") or ""),
            )
            if selected:
                selected_skills.append(selected)
                selected_scores.append(confidence)
                steps.append(step)
                previous_step_id = step_id
            elif include_unrouted:
                steps.append(step)
                previous_step_id = step_id

        distinct_skills = set(selected_skills)
        selected_count = len(selected_skills)
        if selected_count < min_steps or len(distinct_skills) < min_distinct:
            reason = "not-enough-distinct-skills"
            decision = "no-composite"
        else:
            reason = "multiple-routable-subtasks"
            decision = "composite"

        level3_candidate = bool(
            decision == "composite"
            and any(step.execution_hint == "async-domain-mcp" for step in steps)
            and any(
                (profiles.get(step.selected_skill) and set(profiles[step.selected_skill].domains) & {
                    "literature",
                    "protein-sequence",
                    "protein-structure",
                    "docking-interaction",
                    "simulation",
                })
                for step in steps
                if step.selected_skill
            )
        )
        confidence = sum(selected_scores) / max(1, len(selected_scores))
        trace_steps = [step.to_dict() for step in steps]
        base_trace.update(
            {
                "decision": decision,
                "reason": reason,
                "confidence": round(confidence, 6),
                "selected_skill_count": selected_count,
                "distinct_skill_count": len(distinct_skills),
                "level3_candidate": level3_candidate,
                "steps": trace_steps,
                "config_fingerprint": hashlib.sha256(
                    json.dumps(config, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
                ).hexdigest()[:16],
            }
        )
        trace_path = SkillRoutingTraceStore.append_route_trace(base_trace, workdir=workdir, config=config)
        if trace_path:
            base_trace["trace_path"] = trace_path

        return CompositeSkillPlan(
            decision=decision,
            mode=mode,
            reason=reason,
            steps=steps if decision == "composite" else [],
            confidence=confidence,
            trace=base_trace,
            expose_to_agent=expose_to_agent and decision == "composite",
            level3_candidate=level3_candidate,
        )
