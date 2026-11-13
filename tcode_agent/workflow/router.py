"""Registry-driven workflow routing, independent of task domain.

Exact names select templates; free-form intent is handled by discovery.py.
Literal terms only order discovery cards, never exclude a template or execute it.
"""
from __future__ import annotations
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from .clarifications import missing_required_fields_from_contract
from .registry import WorkflowTemplateRegistry
from .input_bindings import bind_workflow_request
# Compatibility exports; only used for explicitly selected legacy agents.
from .legacy_protein_routing import guard_protein_design_brainstorming_route, is_protein_design_brainstorm_mode

def _metadata_bool(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        return default
    return bool(value)


def _metadata_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _metadata_string_items(value: Any, *, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        raw_items = [item.strip() for item in re.split(r"[\n,，;；]+", value)]
    elif isinstance(value, list):
        raw_items = [str(item or "").strip() for item in value]
    else:
        raw_items = []
    items: list[str] = []
    for item in raw_items:
        if item and item not in items:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def _workflow_routing_config(template) -> dict[str, Any]:
    metadata = dict(getattr(template, "metadata", {}) or {})
    raw = metadata.get("routing")
    if not isinstance(raw, dict):
        raw = metadata.get("routing_hints")
    routing = dict(raw or {}) if isinstance(raw, dict) else {}
    configured = bool(routing)
    aliases = _metadata_string_items(metadata.get("aliases"), limit=12)
    aliases = [
        item
        for item in [
            str(getattr(template, "template_key", "") or "").strip(),
            str(getattr(template, "display_name", "") or "").strip(),
            *aliases,
        ]
        if item
    ]
    summary = str(
        routing.get("summary")
        or routing.get("description")
        or getattr(template, "description", "")
        or ""
    ).strip()
    return {
        "configured": configured,
        "enabled": _metadata_bool(routing.get("enabled"), default=True),
        "auto_start": _metadata_bool(routing.get("auto_start"), default=configured),
        "match_threshold": max(
            0.1,
            min(0.95, _metadata_float(routing.get("match_threshold"), default=0.38)),
        ),
        "summary": summary[:500],
        "aliases": list(dict.fromkeys(aliases))[:12],
        "use_when": _metadata_string_items(routing.get("use_when"), limit=4),
        "do_not_use_when": _metadata_string_items(routing.get("do_not_use_when"), limit=4),
        "trigger_terms": _metadata_string_items(routing.get("trigger_terms"), limit=16),
        "block_terms": _metadata_string_items(routing.get("block_terms"), limit=16),
    }


def workflow_routing_config(template) -> dict[str, Any]:
    """Return the normalized routing/applicability contract for a template.

    Keeping this projection public lets the execution boundary reuse exactly the
    same ``use_when``/``do_not_use_when`` rules as the conversational router.
    """

    return dict(_workflow_routing_config(template))



def _matched_terms(text: str, terms: list[str]) -> list[str]:
    lowered = str(text or "").lower()
    return [term for term in terms if term and term.lower() in lowered]


def _alias_in_text(alias: str, text: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9_.-]+", alias):
        return bool(re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(alias) + r"(?![A-Za-z0-9_.-])", text, re.I))
    return alias.lower() in text.lower()


def _explicit_start_request(text: str, card: dict[str, Any]) -> bool:
    # Generic execution syntax, not task-domain vocabulary. Mere mentions such
    # as "这个流程如何工作" must not authorize an execution.
    if text.strip(" `‘’\"。.").casefold() in {a.casefold() for a in card["aliases"]}:
        return True
    # An introductory sentence is not a reason to lose an explicit command.
    # Match complete command sentences, never a quoted/negative mention.
    sentences = [part.strip() for part in re.split(r"[。！？\n]+", text) if part.strip()]
    if len(sentences) > 1:
        return any(_explicit_start_request(sentence, card) for sentence in sentences)
    command = re.match(
        r"^\s*(?:(?:请|帮我|麻烦你|麻烦|现在|好的)[，,\s]*)*"
        r"(?:运行|执行|启动|使用|用|(?:please\s+)?(?:run|execute|start|use)\b)", text, re.I)
    if not command:
        return False
    # The name must be the command's object, not a later explanatory mention
    # such as "请用中文解释 X". Less explicit phrasing goes to semantic discovery.
    remainder = re.sub(
        r"^\s*(?:(?:这个|以下|指定的|名为)\s*)?(?:(?:workflow|工作流|流程)\s*)?[:：\s`‘’“”\"]*",
        "", text[command.end():], flags=re.I,
    )
    return any(re.match(re.escape(alias) + (r"(?![A-Za-z0-9_.-])"
        if re.fullmatch(r"[A-Za-z0-9_.-]+", alias) else ""), candidate, re.I)
        for candidate in (text[command.end():].lstrip(" :：`‘’“”\""), remainder)
        for alias in card["aliases"])


def workflow_routing_cards(message: str = "", *, limit: int | None = 6) -> list[dict[str, Any]]:
    """List registered capabilities. limit=None is exhaustive, not keyword gated."""
    cards = []
    for template in WorkflowTemplateRegistry.list_templates():
        routing = workflow_routing_config(template)
        if not routing["enabled"]:
            continue
        aliases = [a for a in routing["aliases"] if _alias_in_text(a, message)] if message else []
        terms = _matched_terms(message, routing["trigger_terms"])
        blocks = _matched_terms(message, routing["block_terms"])
        score = 0.92 if aliases else min(0.78, 0.12 * len(terms))
        cards.append({
            "template_key": template.template_key, "display_name": template.display_name,
            "domain": template.domain, "summary": routing["summary"],
            "aliases": routing["aliases"], "use_when": routing["use_when"],
            "do_not_use_when": routing["do_not_use_when"], "trigger_terms": routing["trigger_terms"],
            "required_inputs": template.input_contract.get("required", []),
            "optional_inputs": template.input_contract.get("optional", []),
            "required_outputs": template.output_contract.get("required", []),
            "alias_matches": aliases, "trigger_matches": terms, "matched_terms": aliases + terms,
            "blocked_terms": blocks, "score": score, "threshold": routing["match_threshold"],
            "auto_start": routing["auto_start"], "routing_enabled": True,
            "routing_configured": routing["configured"], "explicit_alias_match": bool(aliases),
            "should_start": False,
        })
    cards.sort(key=lambda item: (-item["score"], item["template_key"]))
    return cards if limit is None else cards[:max(1, int(limit))]


@dataclass(slots=True)
class WorkflowRouteDecision:
    should_start: bool
    intent: str = ""
    intent_family: str = ""
    template_key: str = ""
    confidence: float = 0.0
    reason: str = ""
    input_payload: dict[str, Any] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    requested_tool_hints: list[str] = field(default_factory=list)
    recommended_agent: str = "build"
    recommended_scheduler_mode: str = "supervisor"
    should_delegate_to_recommended_agent: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["confidence"] = round(float(self.confidence), 3)
        return payload


def _build_input_payload(text: str, resource_candidates=None, path_payload=None) -> dict[str, Any]:
    """Legacy call signature; no domain fields are parsed by the core."""
    return {"target_description": text.strip()[:2000]} if text.strip() else {}


def detect_workflow_route(message: str, *, resource_context=None, requested_agent_name: str = "",
                          workdir: Optional[str] = None) -> WorkflowRouteDecision:
    text = str(message or "").strip()
    cards = workflow_routing_cards(text, limit=None)
    matches = [c for c in cards if c["explicit_alias_match"]]
    selected = matches[0] if len(matches) == 1 and _explicit_start_request(text, matches[0]) else None
    template = WorkflowTemplateRegistry.get(selected["template_key"]) if selected else None
    payload = _build_input_payload(text)
    if template:
        payload = bind_workflow_request(payload, template.input_contract, text)
    decision = WorkflowRouteDecision(
        should_start=template is not None,
        intent=template.template_key if template else "", intent_family="workflow" if template else "",
        template_key=template.template_key if template else "", confidence=1.0 if template else 0.0,
        reason="用户明确选择已注册 Workflow；启动仍须适用性检查。" if template else "待按注册能力目录判断需求。",
        input_payload=payload,
        missing_required=missing_required_fields_from_contract(payload, template.input_contract) if template else [],
        recommended_agent=requested_agent_name or "build",
        signals={"requested_workflow_template_key": template.template_key if template else "",
                 "explicit_workflow_candidates": [c["template_key"] for c in matches],
                 "workflow_catalog_size": len(cards), "route_analysis_source": "registry",
                 "force_new_run": bool(re.search(
                     r"(?:新建|创建|启动)(?:一个|新的)?(?:独立)?运行|新建独立运行|new\s+(?:independent\s+)?run",
                     text,
                     flags=re.IGNORECASE,
                 )),
                 "requested_agent_name": requested_agent_name},
    )
    return guard_protein_design_brainstorming_route(decision, requested_agent_name, workdir=workdir, message=message)


def looks_like_workflow_follow_up(message: str, *, allow_negative_short_answer: bool = True) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if any(term in text for term in ("继续", "接着", "恢复", "断点", "补充", "路径", "约束", "限制", "成功标准",
                                     "continue", "resume", "constraints", "metrics")):
        return True
    if re.search(r"(?:/|\\|\./)\S+\.[A-Za-z0-9]{1,12}(?:\s|$)", text):
        return True
    return bool(allow_negative_short_answer and len(text) <= 80
                and any(token in text for token in ("无", "没有", "none", "no extra")))


def workflow_run_waiting_for_user(run: Any) -> bool:
    """Return true when a workflow run is likely waiting for user-provided input."""

    if run is None:
        return False
    status = str(getattr(run, "status", "") or "").strip().lower()
    if status == "blocked":
        return True
    metadata = dict(getattr(run, "metadata", {}) or {})
    pending_fields = metadata.get("clarification_pending_fields")
    if isinstance(pending_fields, list) and any(str(item or "").strip() for item in pending_fields):
        return True
    for stage in getattr(run, "stage_states", []) or []:
        stage_status = str(getattr(stage, "status", "") or "").strip().lower()
        if stage_status in {"blocked", "review_pending", "collecting"}:
            return True
        stage_metadata = dict(getattr(stage, "metadata", {}) or {})
        pending_fields = stage_metadata.get("clarification_pending_fields")
        if isinstance(pending_fields, list) and any(str(item or "").strip() for item in pending_fields):
            return True
    return False


def should_auto_route_workflow_follow_up(message: str, run: Any) -> bool:
    """Decide whether a short message should resume an existing workflow run."""

    waiting_for_user = workflow_run_waiting_for_user(run)
    if looks_like_workflow_follow_up(
        message,
        allow_negative_short_answer=waiting_for_user,
    ):
        return True
    return False
