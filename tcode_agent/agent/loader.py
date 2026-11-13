"""AgentLoader: load agent definitions from a JSON directory.

Supports the OpenClaw-style pattern: one JSON file per agent, each declaring
name, description, mode, permission, allowed_subagents, prompt, options, steps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from ..config.manager import ConfigManager
from ..types import AgentInfo, AgentMode
from .agents import BuiltinAgents


class AgentLoader:
    """Load agent definitions from a directory of JSON files."""

    # ── Public API ───────────────────────────────────────────────

    @classmethod
    def default_agents_dir(cls, workdir: Optional[str] = None) -> Path:
        # Agent definitions ship with the TCode package.  A dedicated config
        # root (for example ``.tcode-local``) contains runtime configuration,
        # not another copy of ``tcode_agent/agents``.  Keep the historical
        # config-root override when it actually exists, otherwise fall back to
        # the packaged directory so manager.json and the other built-ins do not
        # disappear merely because TCODE_CONFIG_ROOT is set.
        configured = (
            Path(ConfigManager.get_effective_config_root(workdir or os.getcwd()))
            / "tcode_agent"
            / "agents"
        )
        if configured.is_dir():
            return configured
        return Path(__file__).resolve().parents[1] / "agents"

    @classmethod
    def load_from_directory(
        cls,
        directory: str | Path,
        *,
        source: str = "json-directory",
    ) -> dict[str, AgentInfo]:
        """Load all *.json files from a directory as agent definitions.

        Returns a dict of {agent_name: AgentInfo}.
        """
        agents: dict[str, AgentInfo] = {}
        dir_path = Path(directory)

        if not dir_path.is_dir():
            return agents

        for file_path in sorted(dir_path.glob("*.json")):
            agent = cls.load_one(file_path, source=source)
            if agent is not None:
                agents[agent.name] = agent

        return agents

    @classmethod
    def load_one(
        cls,
        file_path: str | Path,
        *,
        source: str = "json-file",
    ) -> Optional[AgentInfo]:
        """Load a single JSON agent file. Returns None on error."""
        path = Path(file_path)
        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        return cls._parse_agent(data, source=source, json_path=str(path))

    @classmethod
    def load_and_register(
        cls,
        directory: str | Path,
        *,
        source: str = "json-directory",
    ) -> int:
        """Load all agents from directory and register them with BuiltinAgents.

        Returns the number of agents loaded.
        """
        agents = cls.load_from_directory(directory, source=source)
        for name, agent in agents.items():
            BuiltinAgents.register_custom(agent, source)
        return len(agents)

    # ── Parsing ──────────────────────────────────────────────────

    @classmethod
    def _parse_agent(cls, data: dict[str, Any], *, source: str, json_path: str = "") -> Optional[AgentInfo]:
        name = str(data.get("name", "") or "").strip()
        if not name:
            return None

        mode = cls._parse_mode(data.get("mode"))
        allowed_subagents = cls._parse_list(data.get("allowed_subagents", []) or [])

        options = dict(data.get("options", {}) or {})
        options["_source"] = source
        if allowed_subagents:
            options["allowed_subagents"] = allowed_subagents

        return AgentInfo(
            name=name,
            description=str(data.get("description", "") or ""),
            mode=mode,
            native=bool(data.get("native", False)),
            hidden=bool(data.get("hidden", False)),
            temperature=data.get("temperature"),
            top_p=data.get("topP", data.get("top_p")),
            color=data.get("color"),
            permission=dict(data.get("permission", {}) or {}),
            model=data.get("model"),
            variant=data.get("variant"),
            prompt=cls._resolve_prompt(data.get("prompt"), source=source, json_path=json_path),
            options=options,
            steps=data.get("steps"),
        )

    @classmethod
    def _parse_mode(cls, value: Any) -> AgentMode:
        if value is None:
            return AgentMode.PRIMARY
        try:
            return AgentMode(str(value).strip().lower())
        except (ValueError, TypeError):
            return AgentMode.PRIMARY

    @classmethod
    def _parse_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @classmethod
    def _resolve_prompt(
        cls,
        value: Any,
        *,
        source: str = "",
        json_path: str = "",
    ) -> Optional[str]:
        """Resolve the prompt field — supports inline strings and .md file paths.

        Accepted formats:
          "prompt": "inline prompt text"            → returned as-is
          "prompt": "agents/manager_prompt.md"      → .md file path, content read
          "prompt": "./prompts/foo.md"              → .md file path, content read
          "prompt": {"file": "agents/prompt.md"}    → explicit file reference
          "prompt": null / absent                    → None (no custom prompt)

        Paths ending in .md are treated as file references.
        Paths are resolved relative to the JSON file's directory (if json_path is provided).
        """
        if value is None:
            return None

        # Dict format: {"file": "path/to/prompt.md"}
        if isinstance(value, dict):
            file_path = str(value.get("file", "") or "").strip()
            if file_path:
                return cls._read_prompt_file(file_path, json_path=json_path)
            return None

        # String format
        text = str(value).strip()
        if not text:
            return None

        # Detect .md file path — relative or absolute
        if text.endswith(".md"):
            return cls._read_prompt_file(text, json_path=json_path)

        # Plain inline prompt
        return text

    @classmethod
    def _read_prompt_file(cls, file_path: str, *, json_path: str = "") -> str:
        """Read prompt content from a .md file.

        If file_path is relative and json_path is provided, resolve relative to
        the JSON file's directory. Otherwise resolve relative to cwd.
        """
        path = Path(file_path)
        if not path.is_absolute() and json_path:
            json_dir = Path(json_path).parent
            path = (json_dir / path).resolve()
        else:
            path = path.expanduser()

        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        # File not found — return the path as a warning so it doesn't silently fail
        return f"[prompt file not found: {file_path}]"
