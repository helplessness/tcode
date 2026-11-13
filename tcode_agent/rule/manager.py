"""Rule system - Load AGENTS.md and other rule files"""

import os
from typing import Optional
from dataclasses import dataclass

from ..prompt.security import PromptSecurityFinding, scan_prompt_text


@dataclass
class RuleRecord:
    filename: str
    path: str
    content: str
    blocked: bool = False
    findings: list[PromptSecurityFinding] | None = None


class RuleManager:
    """Manages project rules from AGENTS.md and similar files"""

    _rules: dict[str, RuleRecord] = {}
    _loaded: bool = False
    _diagnostics: list[dict[str, object]] = []

    # Rule file names in priority order
    RULE_FILES = [
        "AGENTS.md",
        "CLAUDE.md",
        ".cursor/rules.md",
    ]

    @classmethod
    def load(cls, workdir: str = None) -> Optional[str]:
        """Load rules from workdir"""
        if workdir is None:
            workdir = os.getcwd()
        cls._rules = {}
        cls._loaded = False
        cls._diagnostics = []

        # Try to find rule files in priority order
        for rule_file in cls.RULE_FILES:
            rule_path = os.path.join(workdir, rule_file)
            if os.path.exists(rule_path):
                try:
                    with open(rule_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            scan = scan_prompt_text(content, source=rule_path)
                            record = RuleRecord(
                                filename=rule_file,
                                path=rule_path,
                                content=scan.sanitized_content,
                                blocked=scan.blocked,
                                findings=list(scan.findings or []),
                            )
                            cls._rules[rule_file] = record
                            cls._loaded = True
                            if scan.blocked:
                                cls._diagnostics.append(
                                    {
                                        "filename": rule_file,
                                        "path": rule_path,
                                        "blocked": True,
                                        "findings": [
                                            {"kind": item.kind, "detail": item.detail}
                                            for item in scan.findings
                                        ],
                                    }
                                )
                                print(
                                    f"[RuleManager] Blocked unsafe rule file: {rule_path}"
                                )
                                continue
                            return scan.sanitized_content
                except Exception as e:
                    print(f"Failed to load rules from {rule_path}: {e}")

        return None

    @classmethod
    def get_rules(cls) -> str:
        """Get all loaded rules"""
        safe_rules = [
            item.content
            for item in cls._rules.values()
            if isinstance(item, RuleRecord) and not item.blocked and item.content
        ]
        return "\n\n".join(safe_rules)

    @classmethod
    def diagnostics(cls) -> list[dict[str, object]]:
        return list(cls._diagnostics)

    @classmethod
    def is_loaded(cls) -> bool:
        """Check if rules are loaded"""
        return cls._loaded

    @classmethod
    def clear(cls):
        """Clear loaded rules"""
        cls._rules = {}
        cls._loaded = False
        cls._diagnostics = []
