"""Skill 路由元数据模型与旧格式兼容层。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .manager import SkillInfo


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.replace("，", ",").split(",")]
    elif isinstance(value, dict):
        value = list(value.keys())
    elif not isinstance(value, (list, tuple, set)):
        value = [value]
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _first(metadata: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metadata and metadata.get(key) not in (None, ""):
            return metadata.get(key)
    return None


@dataclass(frozen=True)
class SkillRoutingProfile:
    skill: SkillInfo
    domains: tuple[str, ...]
    intents: tuple[str, ...]
    aliases: tuple[str, ...]
    trigger_keywords: tuple[str, ...]
    positive_examples: tuple[str, ...]
    negative_examples: tuple[str, ...]
    required_inputs: tuple[str, ...]
    optional_inputs: tuple[str, ...]
    excludes: tuple[str, ...]
    conflicts_with: tuple[str, ...]
    depends_on: tuple[str, ...]
    agent_allowlist: tuple[str, ...]
    priority: int
    enabled: bool
    auto_load: bool
    when_to_use: str
    skill_role: str
    target_server: str
    target_tool: str

    @classmethod
    def from_skill(cls, skill: SkillInfo) -> "SkillRoutingProfile":
        metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
        routing = metadata.get("routing") if isinstance(metadata.get("routing"), dict) else {}

        when_raw = _first(metadata, "when_to_use", "when-to-use", "whenToUse")
        when_items = _string_list(when_raw)
        when_to_use = "；".join(when_items)

        legacy_match_terms = _string_list(metadata.get("match_terms"))
        legacy_trigger_keywords = _string_list(metadata.get("trigger_keywords"))
        routing_aliases = _string_list(routing.get("aliases"))
        trigger_keywords = list(dict.fromkeys([*legacy_trigger_keywords, *routing_aliases]))
        aliases = list(dict.fromkeys([skill.name, *routing_aliases, *legacy_match_terms, *legacy_trigger_keywords]))

        positive_examples = _string_list(routing.get("positive_examples"))
        if when_to_use and when_to_use not in positive_examples:
            positive_examples.append(when_to_use)

        try:
            priority = max(0, min(100, int(routing.get("priority", metadata.get("priority", 50)) or 50)))
        except Exception:
            priority = 50

        routing_enabled = routing.get("enabled", metadata.get("enabled", True))
        routing_auto_load = routing.get("auto_load", metadata.get("auto_load", False))
        return cls(
            skill=skill,
            domains=tuple(_string_list(routing.get("domains") or metadata.get("domains") or metadata.get("tags"))),
            intents=tuple(_string_list(routing.get("intents") or metadata.get("intents"))),
            aliases=tuple(aliases),
            trigger_keywords=tuple(trigger_keywords),
            positive_examples=tuple(positive_examples),
            negative_examples=tuple(_string_list(routing.get("negative_examples"))),
            required_inputs=tuple(_string_list(routing.get("required_inputs"))),
            optional_inputs=tuple(_string_list(routing.get("optional_inputs"))),
            excludes=tuple(_string_list(routing.get("excludes"))),
            conflicts_with=tuple(_string_list(routing.get("conflicts_with"))),
            depends_on=tuple(_string_list(routing.get("depends_on"))),
            agent_allowlist=tuple(_string_list(routing.get("agent_allowlist"))),
            priority=priority,
            enabled=bool(routing_enabled),
            auto_load=bool(routing_auto_load),
            when_to_use=when_to_use,
            skill_role=str(routing.get("skill_role") or metadata.get("skill_role") or "").strip(),
            target_server=str(routing.get("target_server") or metadata.get("target_server") or "").strip(),
            target_tool=str(routing.get("target_tool") or metadata.get("target_tool") or "").strip(),
        )

    def applies_to_agent(self, agent_name: str) -> bool:
        if not self.agent_allowlist:
            return True
        normalized = str(agent_name or "").strip().lower()
        return normalized in {item.lower() for item in self.agent_allowlist}

    def search_text(self) -> str:
        """用于 BM25 的短路由文本，不包含完整 Skill 正文。"""

        return "\n".join(
            part
            for part in [
                self.skill.name,
                self.skill.description,
                self.when_to_use,
                " ".join(self.domains),
                " ".join(self.intents),
                " ".join(self.aliases),
                " ".join(self.required_inputs),
                " ".join(self.optional_inputs),
                self.target_server,
                self.target_tool,
                *self.positive_examples,
            ]
            if str(part or "").strip()
        )

    def vector_documents(self) -> list[str]:
        """为一个 Skill 生成多条向量文档，正例单独建向量。"""

        base = "\n".join(
            part
            for part in [
                self.skill.name,
                self.skill.description,
                self.when_to_use,
                f"domains: {' '.join(self.domains)}" if self.domains else "",
                f"intents: {' '.join(self.intents)}" if self.intents else "",
                f"aliases: {' '.join(self.aliases)}" if self.aliases else "",
                f"required inputs: {' '.join(self.required_inputs)}" if self.required_inputs else "",
                self.target_server,
                self.target_tool,
            ]
            if str(part or "").strip()
        )
        documents = [base] if base else [self.skill.name]
        for example in self.positive_examples:
            text = f"{self.skill.name}\n{self.skill.description}\n用户请求示例：{example}"
            if text not in documents:
                documents.append(text)
        return documents

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "domains": list(self.domains),
            "intents": list(self.intents),
            "aliases": list(self.aliases),
            "trigger_keywords": list(self.trigger_keywords),
            "positive_examples": list(self.positive_examples),
            "negative_examples": list(self.negative_examples),
            "required_inputs": list(self.required_inputs),
            "optional_inputs": list(self.optional_inputs),
            "excludes": list(self.excludes),
            "conflicts_with": list(self.conflicts_with),
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "enabled": self.enabled,
            "auto_load": self.auto_load,
            "when_to_use": self.when_to_use,
            "skill_role": self.skill_role,
            "target_server": self.target_server,
            "target_tool": self.target_tool,
        }

