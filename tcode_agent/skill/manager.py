"""Skill system - Load specialized skills"""

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import glob as glob_module
import hashlib
import json
import re
from pathlib import Path

import yaml

from ..runtime.registrations import RegistrationDisposer, RegistrationToken


@dataclass(frozen=True)
class SkillActionArgument:
    """One validated command-line argument exposed by a skill action."""

    name: str
    type: str = "string"
    flag: str = ""
    positional: bool = False
    required: bool = False
    default: Any = None
    choices: tuple[Any, ...] = ()
    must_exist: bool = False
    artifact: bool = False
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillAction:
    """A sandboxed executable entrypoint declared by a skill."""

    name: str
    description: str
    entrypoint: str
    capability: str
    interpreter: str = "auto"
    timeout_seconds: int = 120
    arguments: dict[str, SkillActionArgument] = field(default_factory=dict)

@dataclass
class SkillInfo:
    """Information about a skill"""

    name: str
    description: str
    location: str
    content: str
    source: Optional[str] = None
    plugin: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    actions: dict[str, SkillAction] = field(default_factory=dict)
    skill_id: str = ""
    version: str = "1"
    content_hash: str = ""
    source_kind: str = "workspace"


@dataclass(frozen=True)
class SkillDuplicate:
    """同名 Skill 冲突记录。"""

    name: str
    winner_location: str
    duplicate_location: str
    policy: str
    winner_source: str = ""
    duplicate_source: str = ""


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class SkillManager(metaclass=WorkspaceRegistryMeta):

    """Manages skills - specialized instructions for specific tasks"""
    _workspace_fields = ('_skills', '_skill_candidates', '_skill_sources', '_duplicates', '_plugin_skill_dirs', '_plugin_missing_only_dirs', '_skill_registrations', '_plugin_directory_registrations', '_loaded', '_loaded_roots', '_home_loaded', '_active_workdir', '_last_route_result')

    _skills: dict[str, SkillInfo] = {}
    _skill_candidates: dict[str, list[SkillInfo]] = {}
    _skill_sources: dict[str, str] = {}
    _duplicates: list[SkillDuplicate] = []
    _plugin_skill_dirs: dict[str, list[str]] = {}
    _plugin_missing_only_dirs: dict[str, list[str]] = {}
    _skill_registrations: dict[int, RegistrationToken] = {}
    _plugin_directory_registrations: dict[tuple[str, str, bool], RegistrationToken] = {}
    _loaded: bool = False
    _loaded_roots: set[str] = set()
    _home_loaded: bool = False
    _active_workdir: Optional[str] = None
    _last_route_result: Any = None

    # Default skill directories to scan
    DEFAULT_SKILL_DIRS = [
        ".claude/skills",
        ".agents/skills",
        ".tcode/skills",
        "skills",
    ]

    @classmethod
    def _ensure_initialized(cls) -> None:
        if cls._loaded:
            return
        cls._skills = {}
        cls._skill_candidates = {}
        cls._skill_sources = {}
        cls._duplicates = []
        cls._loaded_roots = set()
        cls._home_loaded = False

    @classmethod
    def _candidate_roots(cls, workdir: Optional[str]) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        raw_values = [workdir, os.environ.get("TCODE_WORKDIR")]
        if not str(workdir or "").strip():
            raw_values.append(os.getcwd())

        for raw in raw_values:
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                base = Path(text).expanduser().resolve()
            except Exception:
                continue

            if base.is_file():
                base = base.parent

            lineage = [base, *base.parents]
            for item in lineage:
                normalized = str(item)
                if normalized in seen:
                    continue
                seen.add(normalized)
                candidates.append(normalized)

        return candidates

    @classmethod
    async def load(cls, base_dirs: list[str] = None, workdir: str = None):
        """Load skills from directories"""
        cls._ensure_initialized()

        if workdir is None:
            workdir = os.getcwd()
        cls._active_workdir = str(Path(workdir).expanduser().resolve())

        from .routing_config import load_skill_routing_config

        routing_config = load_skill_routing_config(workdir)
        catalog_config = (
            routing_config.get("catalog", {})
            if isinstance(routing_config.get("catalog"), dict)
            else {}
        )

        search_dirs = cls.DEFAULT_SKILL_DIRS if base_dirs is None else base_dirs

        if base_dirs is None:
            from .roots import default_skill_roots, project_skill_roots
            from ..agent.agents import BuiltinAgents

            projects = project_skill_roots()
            shared = list(projects)
            shared.extend(default_skill_roots(workdir))
            for agent in BuiltinAgents.list():
                shared.extend(default_skill_roots(workdir, agent.options))
            # Load shared defaults first so normal workspace overrides retain
            # the existing duplicate policy. All callers use this same loader.
            for root in dict.fromkeys(shared):
                normalized = str(root)
                if normalized in cls._loaded_roots or root == Path(workdir).expanduser().resolve():
                    continue
                for search_dir in search_dirs:
                    await cls._scan_directory(str(root / search_dir), source='project' if root in projects else 'configured',
                        source_kind='project' if root in projects else 'explicit-root')
                cls._loaded_roots.add(normalized)

        candidate_roots = cls._candidate_roots(workdir)
        if not bool(catalog_config.get("inherit_parent_skills", True)):
            allowed_roots = {
                str(Path(raw).expanduser().resolve())
                for raw in (workdir, os.environ.get("TCODE_WORKDIR"))
                if str(raw or "").strip()
            }
            candidate_roots = [root for root in candidate_roots if root in allowed_roots]

        primary_root = str(Path(workdir).expanduser().resolve())
        for root in candidate_roots:
            if root in cls._loaded_roots:
                continue
            source_kind = "workspace" if root == primary_root else "parent"
            source_label = (
                "workspace"
                if source_kind == "workspace"
                else f"parent:{Path(root).name or 'root'}"
            )
            for search_dir in search_dirs:
                await cls._scan_directory(
                    os.path.join(root, search_dir),
                    source=source_label,
                    source_kind=source_kind,
                )
            cls._loaded_roots.add(root)

        if not cls._home_loaded and bool(catalog_config.get("include_home_skills", True)):
            home_dir = os.path.expanduser("~")
            for search_dir in search_dirs:
                home_skill_dir = os.path.join(home_dir, search_dir)
                await cls._scan_directory(
                    home_skill_dir,
                    source="home",
                    source_kind="home",
                )
            cls._home_loaded = True

        for source, directories in cls._plugin_skill_dirs.items():
            for directory in directories:
                await cls._scan_directory(
                    directory,
                    source=source,
                    plugin=source,
                    source_kind="plugin",
                )
        for source, directories in cls._plugin_missing_only_dirs.items():
            for directory in directories:
                await cls._scan_directory(
                    directory,
                    source=source,
                    plugin=source,
                    source_kind="plugin",
                    only_if_missing=True,
                )

        cls._loaded = True
        cls.persist_catalog_snapshot(workdir)

    @classmethod
    async def load_explicit_roots(
        cls,
        roots: list[str],
        *,
        base_dirs: list[str] | None = None,
        include_home: bool = False,
    ) -> None:
        """Load skills from exact roots only, without walking parent directories."""
        cls._ensure_initialized()

        if roots and not cls._active_workdir:
            cls._active_workdir = str(Path(roots[0]).expanduser().resolve())

        search_dirs = cls.DEFAULT_SKILL_DIRS if base_dirs is None else base_dirs

        for raw_root in roots or []:
            text = str(raw_root or "").strip()
            if not text:
                continue
            try:
                base = Path(text).expanduser().resolve()
            except Exception:
                continue
            if base.is_file():
                base = base.parent
            normalized = str(base)
            if normalized in cls._loaded_roots:
                continue
            for search_dir in search_dirs:
                await cls._scan_directory(
                    str(base / search_dir),
                    source=f"explicit:{base.name or 'root'}",
                    source_kind="explicit-root",
                )
            cls._loaded_roots.add(normalized)

        if include_home and not cls._home_loaded:
            home_dir = os.path.expanduser("~")
            for search_dir in search_dirs:
                home_skill_dir = os.path.join(home_dir, search_dir)
                await cls._scan_directory(
                    home_skill_dir,
                    source="home",
                    source_kind="home",
                )
            cls._home_loaded = True

        for source, directories in cls._plugin_skill_dirs.items():
            for directory in directories:
                await cls._scan_directory(
                    directory,
                    source=source,
                    plugin=source,
                    source_kind="plugin",
                )
        for source, directories in cls._plugin_missing_only_dirs.items():
            for directory in directories:
                await cls._scan_directory(
                    directory,
                    source=source,
                    plugin=source,
                    source_kind="plugin",
                    only_if_missing=True,
                )

        cls._loaded = True
        cls.persist_catalog_snapshot(cls._active_workdir)

    @classmethod
    async def _scan_directory(
        cls,
        directory: str,
        source: Optional[str] = None,
        plugin: Optional[str] = None,
        source_kind: str = "workspace",
        only_if_missing: bool = False,
        instance_id: str = "",
        on_disposer: Optional[Callable[[RegistrationDisposer], None]] = None,
        raise_errors: bool = False,
    ):
        """Scan a directory for SKILL.md files"""
        if not os.path.isdir(directory):
            return

        # Find all SKILL.md files recursively
        pattern = os.path.join(directory, "**", "SKILL.md")
        matches = sorted(glob_module.glob(pattern, recursive=True))

        for skill_file in matches:
            try:
                skill_info = cls._parse_skill_file(skill_file)
                if skill_info:
                    if source:
                        skill_info.source = source
                    if plugin:
                        skill_info.plugin = plugin
                    skill_info.source_kind = source_kind
                    skill_info.skill_id = cls._build_skill_id(skill_info)
                    if only_if_missing and cls.get(skill_info.name) is not None:
                        winner = cls.get(skill_info.name)
                        if str(Path(winner.location).expanduser().resolve()) == str(
                            Path(skill_info.location).expanduser().resolve()
                        ):
                            continue
                        record = SkillDuplicate(
                            name=skill_info.name,
                            winner_location=str(winner.location),
                            duplicate_location=skill_info.location,
                            policy="keep-existing",
                            winner_source=str(winner.source or ""),
                            duplicate_source=str(skill_info.source or ""),
                        )
                        cls._duplicates.append(record)
                        print(
                            "[SkillManager] 检测到同名 Skill "
                            f"'{skill_info.name}'，策略=keep-existing，"
                            f"生效={winner.location}，冲突={skill_info.location}"
                        )
                    else:
                        cls.register(
                            skill_info,
                            instance_id=instance_id,
                            on_disposer=on_disposer,
                        )
            except Exception as e:
                if raise_errors:
                    raise
                print(f"Failed to load skill from {skill_file}: {e}")

    @classmethod
    def _parse_skill_file(cls, filepath: str) -> Optional[SkillInfo]:
        """Parse a SKILL.md file"""
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # Parse frontmatter (YAML between ---).
        frontmatter: dict[str, Any] = {}
        main_content = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                yaml_content = parts[1]
                main_content = parts[2]
                try:
                    loaded = yaml.safe_load(yaml_content) or {}
                except yaml.YAMLError:
                    if re.search(
                        r"^\s*(actions|runtime):\s*$",
                        yaml_content,
                        flags=re.MULTILINE,
                    ):
                        raise
                    loaded = cls._parse_legacy_frontmatter(yaml_content)
                if not isinstance(loaded, dict):
                    raise ValueError("Skill frontmatter must be a YAML object")
                frontmatter = dict(loaded)

        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")

        if not name:
            # Use filename as name
            skill_dir = os.path.dirname(filepath)
            name = os.path.basename(skill_dir)

        if not name:
            return None

        nested_metadata = frontmatter.get("metadata")
        metadata = dict(nested_metadata) if isinstance(nested_metadata, dict) else {}
        for key, value in frontmatter.items():
            if key not in {"name", "description", "actions", "runtime", "metadata"}:
                metadata[key] = value
        for key in {
            "match_terms",
            "trigger_keywords",
            "tags",
            "allowed-tools",
            "arguments",
            "paths",
        }:
            if key in metadata:
                metadata[key] = cls._normalize_list_value(metadata[key])

        runtime = frontmatter.get("runtime")
        if runtime is None:
            runtime = metadata.get("runtime")
        raw_actions = frontmatter.get("actions")
        if raw_actions is None:
            raw_actions = metadata.get("actions")
        actions = cls._parse_actions(
            raw_actions,
            runtime=runtime,
        )
        metadata.pop("actions", None)
        if runtime is not None:
            metadata["runtime"] = runtime

        version = str(frontmatter.get("version") or metadata.get("version") or "1").strip() or "1"
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return SkillInfo(
            name=name,
            description=description,
            location=filepath,
            content=main_content.strip(),
            metadata=metadata,
            actions=actions,
            version=version,
            content_hash=content_hash,
        )

    @staticmethod
    def _normalize_list_value(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        raw = str(value or "").strip()
        if not raw:
            return []
        return [
            item.strip()
            for item in raw.replace("，", ",").split(",")
            if item.strip()
        ]

    @classmethod
    def _parse_legacy_frontmatter(cls, content: str) -> dict[str, Any]:
        frontmatter: dict[str, Any] = {}
        for line in str(content or "").strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            normalized_key = key.strip()
            frontmatter[normalized_key] = cls._coerce_legacy_value(
                normalized_key,
                value.strip(),
            )
        return frontmatter

    @classmethod
    def _coerce_legacy_value(cls, key: str, value: str) -> Any:
        raw = str(value or "").strip()
        lower = raw.lower()
        if lower in {"true", "false"}:
            return lower == "true"
        if key in {
            "match_terms",
            "trigger_keywords",
            "tags",
            "allowed-tools",
            "arguments",
            "paths",
        }:
            return cls._normalize_list_value(raw)
        return raw

    @classmethod
    def _parse_actions(
        cls,
        raw_actions: Any,
        *,
        runtime: Any = None,
    ) -> dict[str, SkillAction]:
        if raw_actions in (None, ""):
            return {}
        if not isinstance(raw_actions, dict):
            raise ValueError("Skill actions must be a YAML object")

        runtime_cfg = runtime if isinstance(runtime, dict) else {}
        default_capability = str(
            runtime_cfg.get("capability")
            or runtime_cfg.get("default_capability")
            or ""
        ).strip()
        default_timeout = cls._positive_int(runtime_cfg.get("timeout_seconds"), 120)
        parsed: dict[str, SkillAction] = {}

        for raw_name, raw_action in raw_actions.items():
            name = str(raw_name or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
                raise ValueError(f"Invalid skill action name: {name}")
            if not isinstance(raw_action, dict):
                raise ValueError(f"Skill action '{name}' must be a YAML object")

            entrypoint = str(
                raw_action.get("entrypoint")
                or raw_action.get("script")
                or ""
            ).strip()
            if not entrypoint:
                raise ValueError(f"Skill action '{name}' requires entrypoint")
            entrypoint_path = Path(entrypoint)
            if entrypoint_path.is_absolute() or ".." in entrypoint_path.parts:
                raise ValueError(
                    f"Skill action '{name}' entrypoint must be skill-relative"
                )

            capability = str(
                raw_action.get("capability") or default_capability
            ).strip()
            if not capability:
                raise ValueError(f"Skill action '{name}' requires capability")

            raw_arguments = raw_action.get("arguments") or {}
            if not isinstance(raw_arguments, dict):
                raise ValueError(
                    f"Skill action '{name}' arguments must be a YAML object"
                )
            arguments: dict[str, SkillActionArgument] = {}
            alias_owner: dict[str, str] = {}
            for raw_arg_name, raw_arg in raw_arguments.items():
                arg_name = str(raw_arg_name or "").strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", arg_name):
                    raise ValueError(
                        f"Invalid argument name '{arg_name}' in action '{name}'"
                    )
                existing_owner = alias_owner.get(arg_name)
                if existing_owner and existing_owner != arg_name:
                    raise ValueError(
                        f"Argument name '{arg_name}' in action '{name}' conflicts with alias of '{existing_owner}'"
                    )
                alias_owner[arg_name] = arg_name
                arg_cfg = raw_arg if isinstance(raw_arg, dict) else {"type": raw_arg}
                arg_type = str(arg_cfg.get("type") or "string").strip().lower()
                if arg_type not in {
                    "string",
                    "integer",
                    "number",
                    "boolean",
                    "path",
                    "path_list",
                    "string_list",
                }:
                    raise ValueError(
                        f"Unsupported argument type '{arg_type}' in action '{name}'"
                    )
                positional = bool(arg_cfg.get("positional", False))
                flag = str(arg_cfg.get("flag") or "").strip()
                if not positional and not flag:
                    flag = f"--{arg_name.replace('_', '-')}"
                if flag and (
                    not flag.startswith("-")
                    or any(char.isspace() for char in flag)
                ):
                    raise ValueError(
                        f"Invalid flag '{flag}' for argument '{arg_name}'"
                    )
                raw_choices = arg_cfg.get("choices") or []
                choices = tuple(
                    raw_choices if isinstance(raw_choices, list) else [raw_choices]
                )
                raw_aliases = arg_cfg.get("aliases") or []
                if isinstance(raw_aliases, str):
                    raw_aliases = [raw_aliases]
                if not isinstance(raw_aliases, list):
                    raise ValueError(
                        f"Argument aliases for '{arg_name}' in action '{name}' must be a YAML list"
                    )
                aliases: list[str] = []
                for raw_alias in raw_aliases:
                    alias = str(raw_alias or "").strip()
                    if not alias or alias == arg_name:
                        continue
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", alias):
                        raise ValueError(
                            f"Invalid alias '{alias}' for argument '{arg_name}' in action '{name}'"
                        )
                    owner = alias_owner.get(alias)
                    if owner and owner != arg_name:
                        raise ValueError(
                            f"Alias '{alias}' for argument '{arg_name}' in action '{name}' conflicts with '{owner}'"
                        )
                    alias_owner[alias] = arg_name
                    if alias not in aliases:
                        aliases.append(alias)
                arguments[arg_name] = SkillActionArgument(
                    name=arg_name,
                    type=arg_type,
                    flag=flag,
                    positional=positional,
                    required=bool(arg_cfg.get("required", False)),
                    default=arg_cfg.get("default"),
                    choices=choices,
                    must_exist=bool(arg_cfg.get("must_exist", False)),
                    artifact=bool(arg_cfg.get("artifact", False)),
                    aliases=tuple(aliases),
                )

            parsed[name] = SkillAction(
                name=name,
                description=str(raw_action.get("description") or "").strip(),
                entrypoint=entrypoint_path.as_posix(),
                capability=capability,
                interpreter=str(raw_action.get("interpreter") or "auto").strip().lower(),
                timeout_seconds=cls._positive_int(
                    raw_action.get("timeout_seconds"),
                    default_timeout,
                ),
                arguments=arguments,
            )
        return parsed

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            return max(1, int(value))
        except Exception:
            return max(1, int(default))

    @staticmethod
    def _build_skill_id(skill: SkillInfo) -> str:
        source = str(skill.source or skill.source_kind or "workspace").strip() or "workspace"
        safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "-", source).strip("-") or "workspace"
        return f"{safe_source}/{skill.name}@{skill.version or '1'}"

    @classmethod
    def _duplicate_policy(cls) -> str:
        try:
            from .routing_config import load_skill_routing_config

            config = load_skill_routing_config(cls._active_workdir)
            catalog = config.get("catalog", {}) if isinstance(config.get("catalog"), dict) else {}
            policy = str(catalog.get("duplicate_policy") or "keep-last").strip().lower()
        except Exception:
            policy = "keep-last"
        return policy if policy in {"keep-first", "keep-last", "error"} else "keep-last"

    @classmethod
    def _select_candidate(cls, name: str) -> None:
        candidates = cls._skill_candidates.get(name, [])
        if not candidates:
            cls._skills.pop(name, None)
            cls._skill_sources.pop(name, None)
            return
        policy = cls._duplicate_policy()
        winner = candidates[0] if policy == "keep-first" else candidates[-1]
        cls._skills[name] = winner
        cls._skill_sources[name] = str(winner.source or "")

    @classmethod
    def register(
        cls,
        skill: SkillInfo,
        *,
        instance_id: str = "",
        on_disposer: Optional[Callable[[RegistrationDisposer], None]] = None,
    ) -> SkillInfo:
        """注册 Skill，并显式记录同名冲突。"""

        if not skill.skill_id:
            skill.skill_id = cls._build_skill_id(skill)
        candidates = cls._skill_candidates.setdefault(skill.name, [])
        normalized_location = str(Path(skill.location).expanduser().resolve())
        if any(str(Path(item.location).expanduser().resolve()) == normalized_location for item in candidates):
            return cls._skills.get(skill.name) or skill

        policy = cls._duplicate_policy()
        if candidates:
            existing = candidates[0] if policy == "keep-first" else candidates[-1]
            if policy == "error":
                raise ValueError(
                    f"Duplicate skill name '{skill.name}': {existing.location} and {skill.location}"
                )
            winner = existing if policy == "keep-first" else skill
            duplicate = skill if policy == "keep-first" else existing
            record = SkillDuplicate(
                name=skill.name,
                winner_location=winner.location,
                duplicate_location=duplicate.location,
                policy=policy,
                winner_source=str(winner.source or ""),
                duplicate_source=str(duplicate.source or ""),
            )
            cls._duplicates.append(record)
            print(
                "[SkillManager] 检测到同名 Skill "
                f"'{skill.name}'，策略={policy}，生效={winner.location}，冲突={duplicate.location}"
            )
        candidates.append(skill)
        token = RegistrationToken.create(
            registry="skills",
            key=skill.skill_id or skill.name,
            source=str(skill.source or skill.plugin or ""),
            instance_id=instance_id,
        )
        cls._skill_registrations[id(skill)] = token
        cls._select_candidate(skill.name)
        if on_disposer is not None:
            on_disposer(RegistrationDisposer(token, cls.unregister_exact))
        return cls._skills[skill.name]

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        removed_skill: Optional[SkillInfo] = None
        for skill_name, candidates in list(cls._skill_candidates.items()):
            remaining: list[SkillInfo] = []
            for skill in candidates:
                current = cls._skill_registrations.get(id(skill))
                if current == token:
                    removed_skill = skill
                    cls._skill_registrations.pop(id(skill), None)
                    continue
                remaining.append(skill)
            if remaining:
                cls._skill_candidates[skill_name] = remaining
            else:
                cls._skill_candidates.pop(skill_name, None)
            cls._select_candidate(skill_name)
        if removed_skill is None:
            return False
        removed_location = str(removed_skill.location)
        cls._duplicates = [
            item
            for item in cls._duplicates
            if item.winner_location != removed_location
            and item.duplicate_location != removed_location
        ]
        if cls._loaded:
            cls.persist_catalog_snapshot(cls._active_workdir)
        return True

    @classmethod
    def _unregister_plugin_directory_exact(cls, token: RegistrationToken) -> bool:
        matching_key = next(
            (
                key
                for key, current in cls._plugin_directory_registrations.items()
                if current == token
            ),
            None,
        )
        if matching_key is None:
            return False
        source, directory, only_if_missing = matching_key
        target = cls._plugin_missing_only_dirs if only_if_missing else cls._plugin_skill_dirs
        directories = target.get(source, [])
        target[source] = [item for item in directories if item != directory]
        if not target[source]:
            target.pop(source, None)
        cls._plugin_directory_registrations.pop(matching_key, None)
        return True

    @classmethod
    async def register_plugin_directory(
        cls,
        directory: str,
        source: str,
        *,
        only_if_missing: bool = False,
        instance_id: str = "",
        replace: bool = True,
        on_disposer: Optional[Callable[[RegistrationDisposer], None]] = None,
        raise_errors: bool = False,
    ) -> list[RegistrationDisposer]:
        disposers: list[RegistrationDisposer] = []
        normalized_directory = str(Path(directory).expanduser().resolve())
        target = cls._plugin_missing_only_dirs if only_if_missing else cls._plugin_skill_dirs
        directories = target.setdefault(source, [])
        registration_key = (source, normalized_directory, only_if_missing)
        if not replace and registration_key in cls._plugin_directory_registrations:
            raise ValueError(
                f"plugin skill directory conflict: '{normalized_directory}' is already registered from {source}"
            )
        if normalized_directory not in directories:
            directories.append(normalized_directory)
        directory_token = RegistrationToken.create(
            registry="skill_directories",
            key=normalized_directory,
            source=source,
            instance_id=instance_id,
        )
        cls._plugin_directory_registrations[registration_key] = directory_token
        directory_disposer = RegistrationDisposer(
            directory_token,
            cls._unregister_plugin_directory_exact,
        )
        disposers.append(directory_disposer)
        if on_disposer is not None:
            on_disposer(directory_disposer)

        def _capture(disposer: RegistrationDisposer) -> None:
            disposers.append(disposer)
            if on_disposer is not None:
                on_disposer(disposer)

        await cls._scan_directory(
            normalized_directory,
            source=source,
            plugin=source,
            source_kind="plugin",
            only_if_missing=only_if_missing,
            instance_id=instance_id,
            on_disposer=_capture,
            raise_errors=raise_errors,
        )
        if cls._loaded:
            cls.persist_catalog_snapshot(cls._active_workdir)
        return disposers

    @classmethod
    def unregister_by_source(
        cls,
        source: str,
        *,
        legacy_only: bool = False,
    ) -> list[str]:
        removed: list[str] = []
        for target, missing_only in (
            (cls._plugin_skill_dirs, False),
            (cls._plugin_missing_only_dirs, True),
        ):
            kept_directories: list[str] = []
            for directory in target.get(source, []):
                token = cls._plugin_directory_registrations.get(
                    (source, directory, missing_only)
                )
                if legacy_only and token is not None and token.instance_id:
                    kept_directories.append(directory)
                    continue
                removed.append(directory)
                cls._plugin_directory_registrations.pop(
                    (source, directory, missing_only),
                    None,
                )
            if kept_directories:
                target[source] = kept_directories
            else:
                target.pop(source, None)
        for skill_name, candidates in list(cls._skill_candidates.items()):
            remaining = [
                skill
                for skill in candidates
                if not (
                    (skill.source == source or skill.plugin == source)
                    and (
                        not legacy_only
                        or not cls._skill_registrations.get(id(skill))
                        or not cls._skill_registrations[id(skill)].instance_id
                    )
                )
            ]
            for skill in candidates:
                if skill not in remaining:
                    removed.append(skill.name)
                    cls._skill_registrations.pop(id(skill), None)
            if remaining:
                cls._skill_candidates[skill_name] = remaining
            else:
                cls._skill_candidates.pop(skill_name, None)
            cls._select_candidate(skill_name)
        cls._duplicates = [
            item
            for item in cls._duplicates
            if item.winner_source != source and item.duplicate_source != source
        ]
        if cls._loaded:
            cls.persist_catalog_snapshot(cls._active_workdir)
        return removed

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> list[str]:
        normalized = str(instance_id or "").strip()
        if not normalized:
            return []
        removed: list[str] = []
        skill_tokens = [
            token
            for token in cls._skill_registrations.values()
            if token.instance_id == normalized
        ]
        removed.extend(token.key for token in skill_tokens if cls.unregister_exact(token))
        directory_tokens = [
            token
            for token in cls._plugin_directory_registrations.values()
            if token.instance_id == normalized
        ]
        removed.extend(
            token.key
            for token in directory_tokens
            if cls._unregister_plugin_directory_exact(token)
        )
        return removed

    @classmethod
    def get(cls, name: str) -> Optional[SkillInfo]:
        """Get a skill by name"""
        direct = cls._skills.get(name)
        if direct is not None:
            return direct
        normalized = str(name or "").strip()
        for skill in cls._skills.values():
            if skill.skill_id == normalized:
                return skill
        return None

    @classmethod
    def all(cls) -> list[SkillInfo]:
        """Get all available skills"""
        return sorted(cls._skills.values(), key=lambda item: (item.name, item.skill_id))

    @classmethod
    def names(cls) -> list[str]:
        """Get all skill names"""
        return sorted(cls._skills.keys())

    @classmethod
    def duplicates(cls) -> list[SkillDuplicate]:
        return list(cls._duplicates)

    @classmethod
    def catalog_snapshot(cls) -> dict[str, Any]:
        skills = [
            {
                "name": skill.name,
                "skill_id": skill.skill_id or cls._build_skill_id(skill),
                "version": skill.version,
                "content_hash": skill.content_hash,
                "description": skill.description,
                "source": skill.source,
                "source_kind": skill.source_kind,
                "plugin": skill.plugin,
                "location": str(Path(skill.location).expanduser().resolve()),
            }
            for skill in cls.all()
        ]
        duplicate_rows = [
            {
                "name": item.name,
                "winner_location": item.winner_location,
                "duplicate_location": item.duplicate_location,
                "policy": item.policy,
                "winner_source": item.winner_source,
                "duplicate_source": item.duplicate_source,
            }
            for item in cls._duplicates
        ]
        fingerprint_payload = {
            "skills": [(item["skill_id"], item["content_hash"], item["location"]) for item in skills],
            "duplicates": duplicate_rows,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        return {
            "snapshot_id": snapshot_id,
            "skill_count": len(skills),
            "duplicate_count": len(duplicate_rows),
            "active_workdir": cls._active_workdir,
            "loaded_roots": sorted(cls._loaded_roots),
            "skills": skills,
            "duplicates": duplicate_rows,
        }

    @classmethod
    def persist_catalog_snapshot(cls, workdir: Optional[str] = None) -> Optional[str]:
        try:
            from .routing_config import load_skill_routing_config
            from .trace import SkillRoutingTraceStore

            config = load_skill_routing_config(workdir or cls._active_workdir)
            return SkillRoutingTraceStore.write_catalog_snapshot(
                cls.catalog_snapshot(),
                workdir=workdir or cls._active_workdir,
                config=config,
            )
        except Exception:
            return None

    @classmethod
    async def reload(cls, base_dirs: list[str] = None, workdir: str = None):
        """Reload skills"""
        cls._skills = {}
        cls._skill_candidates = {}
        cls._skill_sources = {}
        cls._duplicates = []
        cls._loaded = False
        cls._loaded_roots = set()
        cls._home_loaded = False
        await cls.load(base_dirs, workdir)

    @classmethod
    def clear(cls):
        """Clear all loaded skills"""
        cls._skills = {}
        cls._skill_candidates = {}
        cls._skill_sources = {}
        cls._duplicates = []
        cls._plugin_skill_dirs = {}
        cls._plugin_missing_only_dirs = {}
        cls._loaded = False
        cls._loaded_roots = set()
        cls._home_loaded = False
        cls._active_workdir = None
        cls._last_route_result = None
        try:
            from .index import SkillVectorIndex

            SkillVectorIndex.clear()
        except Exception:
            pass

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        normalized = str(text or "").lower()
        ascii_tokens = re.findall(r"[a-z0-9_+-]{2,}", normalized)
        cjk_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", normalized)
        tokens = [*ascii_tokens]
        for chunk in cjk_chunks:
            tokens.extend([chunk[i : i + 2] for i in range(max(1, len(chunk) - 1))])
            tokens.append(chunk)
        return list(dict.fromkeys(tokens))

    @staticmethod
    def _skill_match_terms(skill: SkillInfo) -> list[str]:
        raw = skill.metadata.get("match_terms", []) if isinstance(skill.metadata, dict) else []
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]
        if not isinstance(raw, list):
            return []
        return [str(item or "").strip() for item in raw if str(item or "").strip()]

    @staticmethod
    def _skill_when_to_use(skill: SkillInfo) -> str:
        metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
        for key in ("when_to_use", "when-to-use", "whenToUse"):
            value = metadata.get(key)
            if isinstance(value, (list, tuple)):
                text = "；".join(str(item or "").strip() for item in value if str(item or "").strip())
                if text:
                    return text
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @classmethod
    def route(
        cls,
        *,
        query: str,
        agent_name: str = "",
        recall_texts: Optional[list[str]] = None,
        skills: Optional[list[SkillInfo]] = None,
        limit: int = 3,
        enabled: bool = True,
        workdir: Optional[str] = None,
        use_vector: bool = True,
    ):
        """运行 P1 混合路由并返回完整决策。"""

        from .router import SkillRouteResult, SkillRouter
        from .routing_config import load_skill_routing_config

        if not enabled:
            return SkillRouteResult(decision="no-skill")
        config = load_skill_routing_config(workdir or cls._active_workdir)
        routing_cfg = config.get("routing", {}) if isinstance(config.get("routing"), dict) else {}
        routing_mode = str(routing_cfg.get("mode") or "active").strip().lower()
        if not bool(config.get("enabled", True)) or routing_mode == "legacy":
            recommendations = cls._legacy_recommend(
                query=query,
                agent_name=agent_name,
                recall_texts=recall_texts,
                limit=limit,
                enabled=enabled,
                skills=skills,
            )
            return SkillRouteResult(
                decision="single" if recommendations else "no-skill",
                recommendations=recommendations,
                candidates=recommendations,
                primary_skill=str(recommendations[0].get("name") or "") if recommendations else "",
                confidence=float(recommendations[0].get("score") or 0.0) if recommendations else 0.0,
            )
        try:
            result = SkillRouter.route(
                query=query,
                skills=list(skills) if skills is not None else cls.all(),
                agent_name=agent_name,
                recall_texts=recall_texts,
                limit=limit,
                workdir=workdir or cls._active_workdir,
                catalog_snapshot=cls.catalog_snapshot(),
                use_vector=use_vector,
            )
        except Exception as exc:
            print(f"[SkillManager] P1 混合路由失败，回退旧版推荐：{exc}")
            recommendations = cls._legacy_recommend(
                query=query,
                agent_name=agent_name,
                recall_texts=recall_texts,
                limit=limit,
                enabled=enabled,
                skills=skills,
            )
            result = SkillRouteResult(
                decision="single" if recommendations else "no-skill",
                recommendations=recommendations,
                candidates=recommendations,
                primary_skill=str(recommendations[0].get("name") or "") if recommendations else "",
                confidence=float(recommendations[0].get("score") or 0.0) if recommendations else 0.0,
                trace={
                    "router_version": "legacy-fallback",
                    "decision": "single" if recommendations else "no-skill",
                    "error": str(exc),
                },
            )
        if routing_mode == "shadow" and result.trace.get("router_version") != "legacy-fallback":
            legacy_recommendations = cls._legacy_recommend(
                query=query,
                agent_name=agent_name,
                recall_texts=recall_texts,
                limit=limit,
                enabled=enabled,
                skills=skills,
            )
            try:
                from .trace import SkillRoutingTraceStore, utc_now_iso

                SkillRoutingTraceStore.append_route_trace(
                    {
                        "event_type": "skill_route_shadow_compare",
                        "created_at": utc_now_iso(),
                        "route_request_id": result.trace.get("request_id"),
                        "catalog_snapshot_id": result.trace.get("catalog_snapshot_id"),
                        "router_version": result.trace.get("router_version"),
                        "new_decision": result.decision,
                        "new_primary_skill": result.primary_skill or None,
                        "legacy_primary_skill": (
                            legacy_recommendations[0].get("name")
                            if legacy_recommendations
                            else None
                        ),
                    },
                    workdir=workdir or cls._active_workdir,
                    config=config,
                )
            except Exception:
                pass
            result = SkillRouteResult(
                decision="single" if legacy_recommendations else "no-skill",
                recommendations=legacy_recommendations,
                candidates=legacy_recommendations,
                primary_skill=(
                    str(legacy_recommendations[0].get("name") or "")
                    if legacy_recommendations
                    else ""
                ),
                confidence=(
                    float(legacy_recommendations[0].get("score") or 0.0)
                    if legacy_recommendations
                    else 0.0
                ),
                trace={
                    **dict(result.trace or {}),
                    "routing_mode": "shadow",
                    "shadow_new_decision": result.decision,
                    "shadow_new_primary_skill": result.primary_skill or None,
                },
            )
        cls._last_route_result = result
        cls.persist_catalog_snapshot(workdir or cls._active_workdir)
        return result

    @classmethod
    def composite_route(
        cls,
        *,
        query: str,
        agent_name: str = "",
        recall_texts: Optional[list[str]] = None,
        skills: Optional[list[SkillInfo]] = None,
        enabled: bool = True,
        workdir: Optional[str] = None,
        use_vector: bool = True,
    ):
        """运行 P2 组合 Skill 计划层，不直接执行任何 Skill 或 MCP。"""

        from .composite import CompositeSkillPlan, SkillCompositePlanner
        from .routing_config import load_skill_routing_config

        if not enabled:
            return CompositeSkillPlan(decision="no-composite", mode="off", reason="disabled")
        config = load_skill_routing_config(workdir or cls._active_workdir)
        composite_cfg = config.get("composite", {}) if isinstance(config.get("composite"), dict) else {}
        if not bool(composite_cfg.get("enabled", False)):
            return CompositeSkillPlan(decision="no-composite", mode="off", reason="disabled")
        try:
            return SkillCompositePlanner.plan(
                query=query,
                skills=list(skills) if skills is not None else cls.all(),
                agent_name=agent_name,
                recall_texts=recall_texts,
                workdir=workdir or cls._active_workdir,
                catalog_snapshot=cls.catalog_snapshot(),
                use_vector=use_vector,
            )
        except Exception as exc:
            print(f"[SkillManager] P2 组合路由失败，忽略组合计划：{exc}")
            return CompositeSkillPlan(
                decision="no-composite",
                mode=str(composite_cfg.get("mode") or "off"),
                reason=f"error: {exc}",
            )

    @classmethod
    def recommend(
        cls,
        *,
        query: str,
        agent_name: str = "",
        recall_texts: Optional[list[str]] = None,
        skills: Optional[list[SkillInfo]] = None,
        limit: int = 3,
        enabled: bool = True,
        workdir: Optional[str] = None,
    ) -> list[dict[str, object]]:
        """兼容旧调用方，返回混合路由产生的推荐列表。"""

        return cls.route(
            query=query,
            agent_name=agent_name,
            recall_texts=recall_texts,
            skills=skills,
            limit=limit,
            enabled=enabled,
            workdir=workdir,
        ).recommendations

    @classmethod
    def last_route_result(cls):
        return cls._last_route_result

    @classmethod
    def _legacy_recommend(
        cls,
        *,
        query: str,
        agent_name: str = "",
        recall_texts: Optional[list[str]] = None,
        skills: Optional[list[SkillInfo]] = None,
        limit: int = 3,
        enabled: bool = True,
    ) -> list[dict[str, object]]:
        if not enabled:
            return []
        query_text = "\n".join(
            [
                str(query or ""),
                str(agent_name or ""),
                *[str(item or "") for item in (recall_texts or [])[:4]],
            ]
        ).strip()
        if not query_text:
            return []

        query_tokens = set(cls._tokenize(query_text))
        query_text_lower = query_text.lower()
        if not query_tokens:
            return []

        scored: list[tuple[float, SkillInfo, list[str], str]] = []
        for skill in list(skills) if skills is not None else cls.all():
            metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
            match_terms = cls._skill_match_terms(skill)
            when_to_use = cls._skill_when_to_use(skill)
            skill_text = "\n".join(
                [
                    skill.name,
                    skill.description,
                    when_to_use,
                    str(metadata.get("target_server", "") or ""),
                    str(metadata.get("target_tool", "") or ""),
                    " ".join(match_terms),
                    skill.content[:1200],
                ]
            )
            skill_tokens = set(cls._tokenize(skill_text))
            overlap = sorted(query_tokens & skill_tokens)
            if not overlap:
                continue
            score = float(len(overlap))
            match_term_hits: list[str] = []
            for term in match_terms:
                term_text = str(term or "").strip().lower()
                if term_text and term_text in query_text_lower:
                    match_term_hits.append(term)
            if match_term_hits:
                score += 2.5 * len(dict.fromkeys(match_term_hits))
            if skill.name.lower() in query_text.lower():
                score += 3.0
            if when_to_use:
                when_overlap = sorted(query_tokens & set(cls._tokenize(when_to_use)))
                if when_overlap:
                    score += 2.0
            target_server = str(metadata.get("target_server", "") or "").strip().lower()
            if target_server and target_server in query_text_lower:
                score += 4.0
            target_tool = str(metadata.get("target_tool", "") or "").strip().lower()
            if target_tool and target_tool in query_text_lower:
                score += 2.0
            if skill.description and any(token in cls._tokenize(skill.description) for token in overlap[:4]):
                score += 1.0
            preview_hits = list(dict.fromkeys(match_term_hits + overlap))[:4]
            scored.append((score, skill, preview_hits, when_to_use))

        scored.sort(key=lambda item: item[0], reverse=True)
        recommendations: list[dict[str, object]] = []
        for score, skill, overlap, when_to_use in scored[: max(0, limit)]:
            reason = f"相关线索：{', '.join(overlap)}" if overlap else "与当前任务描述相关"
            metadata = skill.metadata if isinstance(skill.metadata, dict) else {}
            recommendations.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "reason": reason,
                    "score": score,
                    "overlap_tokens": overlap,
                    "when_to_use": when_to_use,
                    "auto_load": bool(metadata.get("auto_load", False)),
                    "skill_role": str(metadata.get("skill_role", "") or "").strip(),
                    "target_server": str(metadata.get("target_server", "") or "").strip(),
                    "target_tool": str(metadata.get("target_tool", "") or "").strip(),
                    "match_terms": cls._skill_match_terms(skill),
                    "trigger_keywords": cls._normalize_list_value(metadata.get("trigger_keywords", [])),
                }
            )
        return recommendations
