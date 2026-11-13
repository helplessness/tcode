"""Fail-closed biosafety controls for protein-design work.

The model-facing risk reviewer is only one input to this module.  Enforcement
lives here and in the unified tool policy so a prompt change, direct MCP call,
or alternate agent cannot silently bypass a required review.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Optional
from ..sqlite_runtime import connect as sqlite_connect


BiosafetyAction = Literal[
    "allow",
    "review_required",
    "human_review_required",
    "block",
]

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
_VALID_REVIEW_DECISIONS = {"ALLOW", "HUMAN_REVIEW_REQUIRED", "BLOCK"}

_DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "decision_ttl_minutes": 120,
    "protected_workers": ["design", "experiment"],
    "reviewer_workers": ["risk"],
    "sensitive_tool_patterns": [
        "*rfdiffusion*",
        "*ligandmpnn*",
        "*evolvepro*",
        "*mutation-scoring*",
        "*mutation_scoring*",
        "*sequence_mutation*",
    ],
    "sequence_export_extensions": [".fa", ".faa", ".fasta", ".fas"],
    "high_risk_action_terms": [
        "increase virulence",
        "enhance virulence",
        "enhance pathogenicity",
        "immune evasion",
        "expand host range",
        "gain of function",
        "提高毒力",
        "增强毒力",
        "增强致病性",
        "免疫逃逸",
        "扩大宿主范围",
        "功能获得",
    ],
    "high_risk_subject_terms": [
        "pathogen",
        "virus",
        "bacterium",
        "toxin",
        "virulence",
        "病原",
        "病毒",
        "细菌",
        "毒素",
        "毒力",
    ],
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _workdir_path(workdir: Optional[str]) -> Path:
    return Path(workdir or os.getcwd()).expanduser().resolve(strict=False)


def _env_enabled(default: bool) -> bool:
    raw = os.environ.get("TCODE_BIOSAFETY_GATE_ENABLED")
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def load_biosafety_policy(workdir: Optional[str] = None) -> dict[str, Any]:
    """Load workspace policy while retaining safe built-in defaults."""

    policy = dict(_DEFAULT_POLICY)
    configured = str(os.environ.get("TCODE_BIOSAFETY_POLICY_PATH", "") or "").strip()
    path = Path(configured).expanduser() if configured else _workdir_path(workdir) / "config" / "biosafety_policy.json"
    if not path.is_absolute():
        path = _workdir_path(workdir) / path

    config_error = ""
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("biosafety policy root must be an object")
            for key, value in payload.items():
                if key in _DEFAULT_POLICY:
                    policy[key] = value
        except Exception as exc:
            config_error = str(exc)

    policy["enabled"] = _env_enabled(bool(policy.get("enabled", True)))
    policy["_config_path"] = str(path)
    policy["_config_error"] = config_error
    return policy


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def biosafety_scope_hash(scope_text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(scope_text or "").strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def tool_arguments_digest(arguments: Any) -> str:
    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BiosafetyDecision:
    decision_id: str
    team_id: str
    scope_hash: str
    decision: str
    risk_level: str
    reason: str
    matched_rules: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    reviewer_session_id: str = ""
    created_at: str = ""
    expires_at: str = ""

    @property
    def active(self) -> bool:
        expires = _parse_iso(self.expires_at)
        return bool(expires and expires > _utc_now())

    def metadata(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["matched_rules"] = list(self.matched_rules)
        payload["constraints"] = list(self.constraints)
        payload["active"] = self.active
        return payload


@dataclass(frozen=True)
class BiosafetyGateResult:
    action: BiosafetyAction
    reason: str = ""
    decision: Optional[BiosafetyDecision] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class BiosafetyDecisionStore:
    """Workspace-local SQLite store for decisions and sensitive-call audit rows."""

    @classmethod
    def db_path(cls, workdir: Optional[str]) -> Path:
        return _workdir_path(workdir) / ".tcode" / "biosafety" / "decisions.sqlite3"

    @classmethod
    def _connect(cls, workdir: Optional[str]) -> sqlite3.Connection:
        path = cls.db_path(workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite_connect(str(path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS biosafety_decisions (
                decision_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                decision TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                reason TEXT NOT NULL,
                matched_rules_json TEXT NOT NULL,
                constraints_json TEXT NOT NULL,
                reviewer_session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_biosafety_decision_scope
                ON biosafety_decisions(team_id, scope_hash, created_at DESC);
            CREATE TABLE IF NOT EXISTS biosafety_tool_audit (
                event_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        return connection

    @classmethod
    def save(cls, workdir: Optional[str], decision: BiosafetyDecision) -> None:
        with cls._connect(workdir) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO biosafety_decisions (
                    decision_id,team_id,scope_hash,decision,risk_level,reason,
                    matched_rules_json,constraints_json,reviewer_session_id,
                    created_at,expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision.decision_id,
                    decision.team_id,
                    decision.scope_hash,
                    decision.decision,
                    decision.risk_level,
                    decision.reason,
                    _canonical_json(list(decision.matched_rules)),
                    _canonical_json(list(decision.constraints)),
                    decision.reviewer_session_id,
                    decision.created_at,
                    decision.expires_at,
                ),
            )

    @classmethod
    def latest(
        cls,
        workdir: Optional[str],
        *,
        team_id: str,
        scope_hash: str,
    ) -> Optional[BiosafetyDecision]:
        if not str(team_id or "").strip() or not str(scope_hash or "").strip():
            return None
        with cls._connect(workdir) as connection:
            row = connection.execute(
                """
                SELECT * FROM biosafety_decisions
                WHERE team_id = ? AND scope_hash = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(team_id), str(scope_hash)),
            ).fetchone()
        if row is None:
            return None
        decision = BiosafetyDecision(
            decision_id=str(row["decision_id"]),
            team_id=str(row["team_id"]),
            scope_hash=str(row["scope_hash"]),
            decision=str(row["decision"]),
            risk_level=str(row["risk_level"]),
            reason=str(row["reason"]),
            matched_rules=tuple(json.loads(str(row["matched_rules_json"]) or "[]")),
            constraints=tuple(json.loads(str(row["constraints_json"]) or "[]")),
            reviewer_session_id=str(row["reviewer_session_id"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
        )
        return decision if decision.active else None

    @classmethod
    def record_tool_event(
        cls,
        workdir: Optional[str],
        *,
        event_id: str,
        team_id: str,
        scope_hash: str,
        decision_id: str,
        tool_name: str,
        arguments_digest: str,
        action: str,
        reason: str,
    ) -> None:
        with cls._connect(workdir) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO biosafety_tool_audit (
                    event_id,team_id,scope_hash,decision_id,tool_name,
                    arguments_digest,action,reason,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    team_id,
                    scope_hash,
                    decision_id,
                    tool_name,
                    arguments_digest,
                    action,
                    reason,
                    _iso(_utc_now()),
                ),
            )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    raw = str(text or "")
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    candidates.append(raw)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        stripped = candidate.strip()
        starts = [0] if stripped.startswith("{") else []
        starts.extend(match.start() for match in re.finditer(r"\{", stripped))
        for start in dict.fromkeys(starts):
            try:
                value, _ = decoder.raw_decode(stripped[start:])
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict) and "decision" in value:
                return value
    return None


def _normalize_review_decision(value: Any) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in {"PASS", "APPROVE", "APPROVED"}:
        return "ALLOW"
    if normalized in {"HUMAN_REVIEW", "REVIEW", "ESCALATE"}:
        return "HUMAN_REVIEW_REQUIRED"
    if normalized in {"DENY", "DENIED", "REJECT", "REJECTED"}:
        return "BLOCK"
    return normalized


def _deterministic_high_risk_rules(scope_text: str, policy: dict[str, Any]) -> list[str]:
    text = str(scope_text or "").lower()
    actions = [term for term in _string_list(policy.get("high_risk_action_terms")) if term.lower() in text]
    subjects = [term for term in _string_list(policy.get("high_risk_subject_terms")) if term.lower() in text]
    if not actions or not subjects:
        return []
    return [f"high_risk_action:{term}" for term in actions] + [f"high_risk_subject:{term}" for term in subjects]


def build_risk_review_contract(*, scope_hash: str, scope_text: str) -> str:
    preview = re.sub(r"\s+", " ", str(scope_text or "").strip())[:1200]
    return (
        "\n\n# Mandatory Biosafety Review Contract\n"
        f"- scope_hash: {scope_hash}\n"
        f"- scope_summary: {preview}\n"
        "只评估该 scope，不执行序列生成、实验、Shell、写文件或高影响 MCP。\n"
        "必须返回一个 JSON 对象（可放在 ```json 代码块中），字段如下：\n"
        '{"decision":"ALLOW|HUMAN_REVIEW_REQUIRED|BLOCK",'
        '"risk_level":"low|medium|high|prohibited",'
        '"reason":"...","matched_rules":[],"constraints":[]}\n'
        "如果信息不足，使用 HUMAN_REVIEW_REQUIRED；不要默认放行。"
    )


def record_risk_review(
    *,
    workdir: Optional[str],
    team_id: str,
    scope_hash: str,
    scope_text: str,
    result_text: str,
    reviewer_session_id: str = "",
) -> BiosafetyGateResult:
    policy = load_biosafety_policy(workdir)
    if not policy.get("enabled", True):
        return BiosafetyGateResult(action="allow", reason="biosafety_gate_disabled")
    if policy.get("_config_error"):
        return BiosafetyGateResult(
            action="block",
            reason=f"Biosafety policy is invalid: {policy['_config_error']}",
            metadata={"policy_config_error": True},
        )

    payload = _extract_json_object(result_text)
    if payload is None:
        return BiosafetyGateResult(
            action="review_required",
            reason="Risk reviewer did not return the required structured decision.",
            metadata={"review_parse_error": True},
        )
    decision_value = _normalize_review_decision(payload.get("decision"))
    if decision_value not in _VALID_REVIEW_DECISIONS:
        return BiosafetyGateResult(
            action="review_required",
            reason="Risk reviewer returned an unsupported decision.",
            metadata={"review_schema_error": True},
        )

    matched_rules = _string_list(payload.get("matched_rules"))
    deterministic_rules = _deterministic_high_risk_rules(scope_text, policy)
    if deterministic_rules:
        decision_value = "BLOCK"
        matched_rules.extend(rule for rule in deterministic_rules if rule not in matched_rules)

    now = _utc_now()
    try:
        ttl_minutes = max(1, min(int(policy.get("decision_ttl_minutes", 120)), 1440))
    except (TypeError, ValueError):
        ttl_minutes = 120
    decision = BiosafetyDecision(
        decision_id=f"bio-{uuid.uuid4().hex}",
        team_id=str(team_id or "").strip(),
        scope_hash=str(scope_hash or biosafety_scope_hash(scope_text)).strip(),
        decision=decision_value,
        risk_level=str(payload.get("risk_level") or "unknown").strip().lower(),
        reason=str(payload.get("reason") or "").strip() or "Risk review completed.",
        matched_rules=tuple(matched_rules),
        constraints=tuple(_string_list(payload.get("constraints"))),
        reviewer_session_id=str(reviewer_session_id or "").strip(),
        created_at=_iso(now),
        expires_at=_iso(now + timedelta(minutes=ttl_minutes)),
    )
    try:
        BiosafetyDecisionStore.save(workdir, decision)
    except Exception as exc:
        return BiosafetyGateResult(
            action="block",
            reason=f"Failed to persist biosafety decision: {exc}",
            metadata={"decision_store_error": True},
        )

    action: BiosafetyAction = "allow"
    if decision.decision == "BLOCK":
        action = "block"
    elif decision.decision == "HUMAN_REVIEW_REQUIRED":
        action = "human_review_required"
    return BiosafetyGateResult(
        action=action,
        reason=decision.reason,
        decision=decision,
        metadata={"biosafety_decision": decision.metadata()},
    )


def evaluate_worker_dispatch(
    *,
    workdir: Optional[str],
    team_id: str,
    worker_name: str,
    scope_hash: str,
) -> BiosafetyGateResult:
    policy = load_biosafety_policy(workdir)
    if not policy.get("enabled", True):
        return BiosafetyGateResult(action="allow", reason="biosafety_gate_disabled")
    protected = {item.lower() for item in _string_list(policy.get("protected_workers"))}
    normalized_worker = str(worker_name or "").strip().lower().replace("-", "_")
    worker_aliases = {
        "protein_design": "design",
        "protein_experiment": "experiment",
    }
    protected_name = worker_aliases.get(normalized_worker, normalized_worker)
    if protected_name not in protected:
        return BiosafetyGateResult(action="allow", reason="worker_not_protected")
    if policy.get("_config_error"):
        return BiosafetyGateResult(
            action="block",
            reason=f"Biosafety policy is invalid: {policy['_config_error']}",
            metadata={"policy_config_error": True},
        )
    try:
        decision = BiosafetyDecisionStore.latest(
            workdir,
            team_id=str(team_id or "").strip(),
            scope_hash=str(scope_hash or "").strip(),
        )
    except Exception as exc:
        return BiosafetyGateResult(
            action="block",
            reason=f"Biosafety decision store is unavailable: {exc}",
            metadata={"decision_store_error": True},
        )
    if decision is None:
        return BiosafetyGateResult(
            action="review_required",
            reason="A current risk-worker decision is required before this worker can run.",
            metadata={"required_worker": "risk", "scope_hash": scope_hash},
        )
    if decision.decision == "HUMAN_REVIEW_REQUIRED":
        approved = _find_scope_human_review(
            workdir=workdir,
            team_id=str(team_id or "").strip(),
            scope_hash=str(scope_hash or "").strip(),
            decision_id=decision.decision_id,
        )
        if approved is not None:
            return BiosafetyGateResult(
                action="allow",
                reason="Biosafety human review approved for this scope.",
                decision=decision,
                metadata={
                    "biosafety_decision": decision.metadata(),
                    "human_review_approved": True,
                    "human_review_identifier": str(approved.get("identifier") or ""),
                },
            )
        return BiosafetyGateResult(
            action="human_review_required",
            reason=decision.reason,
            decision=decision,
            metadata={"biosafety_decision": decision.metadata()},
        )
    if decision.decision != "ALLOW":
        return BiosafetyGateResult(
            action="block",
            reason=decision.reason,
            decision=decision,
            metadata={"biosafety_decision": decision.metadata()},
        )
    return BiosafetyGateResult(
        action="allow",
        reason=decision.reason,
        decision=decision,
        metadata={"biosafety_decision": decision.metadata()},
    )


def _walk_strings(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            values.append(str(key))
            values.extend(_walk_strings(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            values.extend(_walk_strings(nested))
    elif value is not None:
        values.append(str(value))
    return values


def _looks_like_sequence_export(tool_name: str, arguments: Any, policy: dict[str, Any]) -> bool:
    if str(tool_name or "").strip().lower() not in {"write", "edit"}:
        return False
    strings = _walk_strings(arguments)
    extensions = tuple(item.lower() for item in _string_list(policy.get("sequence_export_extensions")))
    has_sequence_path = any(str(value).lower().endswith(extensions) for value in strings) if extensions else False
    if not has_sequence_path:
        return False
    compact = "".join(strings).upper()
    return bool(re.search(r"[ACDEFGHIKLMNPQRSTVWY]{25,}", compact))


def _sensitive_tool_reason(tool_name: str, arguments: Any, policy: dict[str, Any]) -> str:
    normalized_tool = str(tool_name or "").strip().lower()
    if (
        normalized_tool == "protein_candidate"
        and isinstance(arguments, dict)
        and str(arguments.get("action") or "").strip().lower() == "promote"
    ):
        return "candidate_promotion"
    # These built-in control tools only ask/record text; mentioning a runner
    # is not executing it. Explicit patterns targeting their names still apply.
    subject = (normalized_tool if normalized_tool in {'question', 'goal_checkpoint'}
               else " ".join([str(tool_name or ""), *_walk_strings(arguments)]).lower())
    for pattern in _string_list(policy.get("sensitive_tool_patterns")):
        if fnmatch.fnmatch(subject, pattern.lower()):
            return f"sensitive_tool_pattern:{pattern}"
    if _looks_like_sequence_export(tool_name, arguments, policy):
        return "sequence_export"
    return ""


def _message_role(message: Any) -> str:
    role = getattr(message, "role", "")
    return str(getattr(role, "value", role) or "").strip().lower()


def _latest_user_scope(messages: Any) -> str:
    for message in reversed(list(messages or [])):
        if _message_role(message) != "user":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _scope_identity(context: Any) -> tuple[str, str]:
    extra = getattr(context, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    resources = extra.get("resource_context")
    resources = resources if isinstance(resources, dict) else {}
    marker = resources.get("_biosafety")
    marker = marker if isinstance(marker, dict) else {}
    team_id = str(
        marker.get("team_id")
        or extra.get("parent_session_id")
        or getattr(context, "session_id", "")
        or ""
    ).strip()
    scope_hash = str(marker.get("scope_hash") or "").strip()
    if not scope_hash:
        scope_hash = biosafety_scope_hash(_latest_user_scope(getattr(context, "messages", [])))
    return team_id, scope_hash


def biosafety_human_review_arguments(
    *,
    team_id: str,
    scope_hash: str,
    decision_id: str,
) -> dict[str, str]:
    return {
        "team_id": str(team_id or "").strip(),
        "scope_hash": str(scope_hash or "").strip(),
        "decision_id": str(decision_id or "").strip(),
    }


def _find_scope_human_review(
    *,
    workdir: Optional[str],
    team_id: str,
    scope_hash: str,
    decision_id: str,
) -> Optional[dict[str, Any]]:
    """Reuse the existing exact-argument human-review approval store."""

    try:
        from ..mcp._utils import _find_approved_review_for_call_local

        context = SimpleNamespace(extra={"workdir": str(workdir or "")})
        return _find_approved_review_for_call_local(
            tool_server="biosafety-gate",
            tool_name="approve_scope",
            tool_args=biosafety_human_review_arguments(
                team_id=team_id,
                scope_hash=scope_hash,
                decision_id=decision_id,
            ),
            context=context,
        )
    except Exception:
        return None


def evaluate_tool_call(
    *,
    tool_name: str,
    arguments: Any,
    context: Any,
) -> BiosafetyGateResult:
    extra = getattr(context, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    workdir = str(
        extra.get("control_workdir") or extra.get("workdir") or ""
    ).strip() or None
    policy = load_biosafety_policy(workdir)
    if not policy.get("enabled", True):
        return BiosafetyGateResult(action="allow", reason="biosafety_gate_disabled")

    sensitive_reason = _sensitive_tool_reason(tool_name, arguments, policy)
    if not sensitive_reason:
        return BiosafetyGateResult(action="allow", reason="tool_not_biosafety_sensitive")
    team_id, scope_hash = _scope_identity(context)
    base_metadata = {
        "biosafety_sensitive": True,
        "biosafety_trigger": sensitive_reason,
        "biosafety_scope_hash": scope_hash,
    }
    if policy.get("_config_error"):
        return BiosafetyGateResult(
            action="block",
            reason=f"Biosafety policy is invalid: {policy['_config_error']}",
            metadata={**base_metadata, "policy_config_error": True},
        )

    try:
        decision = BiosafetyDecisionStore.latest(
            workdir,
            team_id=team_id,
            scope_hash=scope_hash,
        )
    except Exception as exc:
        return BiosafetyGateResult(
            action="block",
            reason=f"Biosafety decision store is unavailable: {exc}",
            metadata={**base_metadata, "decision_store_error": True},
        )

    action: BiosafetyAction = "allow" if decision and decision.decision == "ALLOW" else "block"
    reason = decision.reason if decision else "Sensitive tool call requires a current risk-worker decision."
    decision_id = decision.decision_id if decision else ""
    human_review: Optional[dict[str, Any]] = None
    if decision and decision.decision == "HUMAN_REVIEW_REQUIRED":
        human_review = _find_scope_human_review(
            workdir=workdir,
            team_id=team_id,
            scope_hash=scope_hash,
            decision_id=decision.decision_id,
        )
        if human_review is not None:
            action = "allow"
            reason = "Biosafety human review approved for this scope."
        else:
            action = "human_review_required"
    event_id = str(getattr(context, "call_id", "") or "").strip() or f"bioevent-{uuid.uuid4().hex}"
    try:
        BiosafetyDecisionStore.record_tool_event(
            workdir,
            event_id=event_id,
            team_id=team_id,
            scope_hash=scope_hash,
            decision_id=decision_id,
            tool_name=str(tool_name or ""),
            arguments_digest=tool_arguments_digest(arguments),
            action=action,
            reason=reason,
        )
    except Exception as exc:
        return BiosafetyGateResult(
            action="block",
            reason=f"Biosafety audit store is unavailable: {exc}",
            decision=decision,
            metadata={**base_metadata, "audit_store_error": True},
        )

    metadata = dict(base_metadata)
    metadata["tool_arguments_digest"] = tool_arguments_digest(arguments)
    if decision:
        metadata["biosafety_decision"] = decision.metadata()
    if human_review is not None:
        metadata["human_review_approved"] = True
        metadata["human_review_identifier"] = str(human_review.get("identifier") or "")
    return BiosafetyGateResult(
        action=action,
        reason=reason,
        decision=decision,
        metadata=metadata,
    )
