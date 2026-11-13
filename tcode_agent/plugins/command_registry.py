"""Registry for plugin-provided prompt commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..runtime.registrations import RegistrationDisposer, RegistrationToken


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    metadata: dict[str, str] = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            yaml_content = parts[1]
            body = parts[2]
            for line in yaml_content.strip().splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
    return metadata, body.strip()


@dataclass
class PluginCommand:
    name: str
    plugin: str
    description: str
    template: str
    source: str
    argument_hint: str = ""
    agent: str = ""
    model: str = ""
    subtask: bool = False

    def render(self, arguments: str) -> str:
        rendered = self.template.replace("$ARGUMENTS", arguments)
        rendered = rendered.replace("${user_message}", arguments)
        return rendered

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "plugin": self.plugin,
            "description": self.description,
            "argument_hint": self.argument_hint,
            "agent": self.agent,
            "model": self.model,
            "subtask": self.subtask,
            "source": self.source,
        }


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class PluginCommandRegistry(metaclass=WorkspaceRegistryMeta):
    _workspace_fields = ('_commands', '_sources', '_registrations')

    _commands: dict[str, PluginCommand] = {}
    _sources: dict[str, str] = {}
    _registrations: dict[str, RegistrationToken] = {}
    _slash_pattern = re.compile(r"^/(?P<name>[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)(?:\s+(?P<args>[\s\S]*))?$")

    @classmethod
    def register(
        cls,
        command: PluginCommand,
        source: str,
        *,
        instance_id: str = "",
        replace: bool = True,
    ) -> RegistrationDisposer:
        if not replace and command.name in cls._commands:
            existing_source = cls._sources.get(command.name, "unknown")
            raise ValueError(
                f"plugin command name conflict: '{command.name}' already registered from {existing_source}"
            )
        token = RegistrationToken.create(
            registry="commands",
            key=command.name,
            source=source,
            instance_id=instance_id,
        )
        cls._commands[command.name] = command
        cls._sources[command.name] = source
        cls._registrations[command.name] = token
        return RegistrationDisposer(token, cls.unregister_exact)

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        current = cls._registrations.get(token.key)
        if current != token:
            return False
        cls._commands.pop(token.key, None)
        cls._sources.pop(token.key, None)
        cls._registrations.pop(token.key, None)
        return True

    @classmethod
    def get(cls, name: str) -> Optional[PluginCommand]:
        return cls._commands.get(name)

    @classmethod
    def list_all(cls) -> list[PluginCommand]:
        return list(cls._commands.values())

    @classmethod
    def unregister_by_source(
        cls,
        source: str,
        *,
        legacy_only: bool = False,
    ) -> list[str]:
        to_remove = [
            name
            for name, item_source in cls._sources.items()
            if item_source == source
            and (
                not legacy_only
                or not cls._registrations.get(name)
                or not cls._registrations[name].instance_id
            )
        ]
        for name in to_remove:
            cls._commands.pop(name, None)
            cls._sources.pop(name, None)
            cls._registrations.pop(name, None)
        return to_remove

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> list[str]:
        normalized = str(instance_id or "").strip()
        if not normalized:
            return []
        tokens = [token for token in cls._registrations.values() if token.instance_id == normalized]
        return [token.key for token in tokens if cls.unregister_exact(token)]

    @classmethod
    def resolve_message(cls, message: str) -> Optional[dict[str, Any]]:
        raw = str(message or "").strip()
        match = cls._slash_pattern.match(raw)
        if not match:
            return None
        name = match.group("name")
        args = (match.group("args") or "").strip()
        command = cls.get(name)
        if not command:
            return None
        return {
            "command": command,
            "name": command.name,
            "arguments": args,
            "rendered_message": command.render(args),
        }


def load_commands_from_directory(directory: Path, plugin_name: str) -> list[PluginCommand]:
    commands: list[PluginCommand] = []
    if not directory.is_dir():
        return commands
    for command_file in sorted(directory.glob("*.md")):
        metadata, body = _parse_frontmatter(command_file.read_text(encoding="utf-8"))
        command_name = f"{plugin_name}:{command_file.stem}"
        commands.append(
            PluginCommand(
                name=command_name,
                plugin=plugin_name,
                description=str(metadata.get("description") or "").strip(),
                template=body,
                source=str(command_file),
                argument_hint=str(metadata.get("argument-hint") or metadata.get("argument_hint") or "").strip(),
                agent=str(metadata.get("agent") or "").strip(),
                model=str(metadata.get("model") or "").strip(),
                subtask=str(metadata.get("subtask") or "false").strip().lower() in {"1", "true", "yes", "on"},
            )
        )
    return commands
