"""团队路由策略。

这里把“manager 收到一个任务时该委派给哪个团队”从 app.py 里拆出来。
后面新增团队时，尽量只注册新规则，不再继续堆 if/else。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..runtime.registrations import RegistrationDisposer, RegistrationToken
from .catalog import TeamCatalog


@dataclass
class TeamRouteRequest:
    message: str
    requested_agent_name: str = ""
    requested_execution_profile: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TeamRouteDecision:
    team_key: str
    agent_name: str
    display_name: str
    slug: str
    reason: str
    route_rule: str
    route_source: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "team_key": self.team_key,
            "agent_name": self.agent_name,
            "display_name": self.display_name,
            "slug": self.slug,
            "reason": self.reason,
            "route_rule": self.route_rule,
            "route_source": self.route_source,
        }
        if self.metadata:
            payload["route_metadata"] = dict(self.metadata)
        return payload


@dataclass
class TeamRouteRule:
    name: str
    team_key: str
    resolver: Callable[[TeamRouteRequest], Optional[bool | dict[str, Any]]]
    priority: int = 100
    source: str = "builtin"
    order: int = 0
    registration_id: str = ""
    instance_id: str = ""


class TeamRoutingPolicy:
    _rules: list[TeamRouteRule] = []
    _rule_order = 0

    @classmethod
    def register_rule(
        cls,
        rule: TeamRouteRule,
        *,
        source: Optional[str] = None,
        instance_id: str = "",
        replace: bool = True,
    ) -> RegistrationDisposer:
        normalized_source = str(source or rule.source or "builtin").strip() or "builtin"
        if not replace and any(item.name == rule.name for item in cls._rules):
            raise ValueError(f"team route rule conflict: '{rule.name}' is already registered")
        cls.unregister_rule(rule.name, source=normalized_source)
        token = RegistrationToken.create(
            registry="team_routing",
            key=rule.name,
            source=normalized_source,
            instance_id=instance_id,
        )
        cls._rule_order += 1
        rule.source = normalized_source
        rule.order = cls._rule_order
        rule.registration_id = token.registration_id
        rule.instance_id = token.instance_id
        cls._rules.append(rule)
        return RegistrationDisposer(token, cls.unregister_exact)

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        before = len(cls._rules)
        cls._rules = [
            rule
            for rule in cls._rules
            if RegistrationToken(
                registry="team_routing",
                key=rule.name,
                source=rule.source,
                instance_id=rule.instance_id,
                registration_id=rule.registration_id,
            )
            != token
        ]
        return len(cls._rules) != before

    @classmethod
    def unregister_rule(cls, name: str, *, source: Optional[str] = None) -> None:
        normalized_name = str(name or "").strip()
        normalized_source = str(source or "").strip()
        if not normalized_name:
            return
        cls._rules = [
            rule
            for rule in cls._rules
            if not (
                rule.name == normalized_name
                and (not normalized_source or rule.source == normalized_source)
            )
        ]

    @classmethod
    def unregister_by_source(
        cls,
        source: str,
        *,
        legacy_only: bool = False,
    ) -> list[str]:
        normalized = str(source or "").strip()
        if not normalized:
            return []
        removed = [
            rule.name
            for rule in cls._rules
            if rule.source == normalized and (not legacy_only or not rule.instance_id)
        ]
        cls._rules = [
            rule
            for rule in cls._rules
            if not (
                rule.source == normalized
                and (not legacy_only or not rule.instance_id)
            )
        ]
        return removed

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> list[str]:
        normalized = str(instance_id or "").strip()
        if not normalized:
            return []
        tokens = [
            RegistrationToken(
                registry="team_routing",
                key=rule.name,
                source=rule.source,
                instance_id=rule.instance_id,
                registration_id=rule.registration_id,
            )
            for rule in cls._rules
            if rule.instance_id == normalized
        ]
        return [token.key for token in tokens if cls.unregister_exact(token)]

    @classmethod
    def list_rules(cls) -> list[dict[str, Any]]:
        ordered = sorted(cls._rules, key=lambda item: (-item.priority, item.order))
        return [
            {
                "name": rule.name,
                "team_key": rule.team_key,
                "priority": rule.priority,
                "source": rule.source,
            }
            for rule in ordered
        ]

    @classmethod
    def route(
        cls,
        request: TeamRouteRequest,
        *,
        available_only: bool = True,
    ) -> Optional[TeamRouteDecision]:
        ordered = sorted(cls._rules, key=lambda item: (-item.priority, item.order))
        for rule in ordered:
            entry = TeamCatalog.get_by_key(rule.team_key, available_only=available_only)
            if entry is None:
                continue
            try:
                matched = rule.resolver(request)
            except Exception:
                continue
            if not matched:
                continue

            payload = entry.delegation_payload()
            metadata: dict[str, Any] = {}
            if isinstance(matched, dict):
                if matched.get("agent_name"):
                    payload["agent_name"] = str(matched.get("agent_name") or "").strip()
                if matched.get("display_name"):
                    payload["display_name"] = str(matched.get("display_name") or "").strip()
                if matched.get("slug"):
                    payload["slug"] = str(matched.get("slug") or "").strip()
                if matched.get("reason"):
                    payload["reason"] = str(matched.get("reason") or "").strip()
                if isinstance(matched.get("metadata"), dict):
                    metadata = dict(matched.get("metadata") or {})

            return TeamRouteDecision(
                team_key=entry.key,
                agent_name=str(payload.get("agent_name") or entry.runtime_agent),
                display_name=str(payload.get("display_name") or entry.display_name),
                slug=str(payload.get("slug") or entry.slug),
                reason=str(payload.get("reason") or entry.delegation_reason or entry.display_name),
                route_rule=rule.name,
                route_source=rule.source,
                metadata=metadata,
            )
        return None
