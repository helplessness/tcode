"""Strict, pre-import validation for TCode plugin Manifest v2 packages."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from .permissions import PluginPermission, PluginPermissionScope


PLUGIN_API_VERSION = "1.0"
PLUGIN_SDK_VERSION = "1.6.0"
PLUGIN_MANIFEST_FILENAME = "plugin.json"
SUPPORTED_PLUGIN_RUNTIMES = frozenset({"python"})
SUPPORTED_PLUGIN_SCOPES = frozenset({"workspace"})
SUPPORTED_REQUESTED_TRUST = frozenset(
    {"builtin", "trusted", "isolated", "quarantined"}
)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_SERVICE_KEY_PATTERN = re.compile(
    r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+@[1-9][0-9]*$"
)
_ENTRY_SYMBOL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_REQUIRED_FIELDS = frozenset(
    {
        "name",
        "version",
        "apiVersion",
        "sdk",
        "python",
        "runtime",
        "entry",
        "provides",
        "requires",
        "optional",
        "permissions",
        "scope",
        "configSchema",
        "requestedTrust",
    }
)
_OPTIONAL_FIELDS = frozenset(
    {
        "description",
        "author",
        "homepage",
        "commands",
        "skills",
        "agents",
        "mcpServers",
        "asyncTargets",
        "workflows",
        "contributes",
        "desktopRuntime",
    }
)
_SUPPORTED_CONTRIBUTIONS = frozenset({"tools", "prompt", "events", "verifiers"})
_FORBIDDEN_TRUST_FIELDS = frozenset(
    {"trust", "effectiveTrust", "grantedPermissions"}
)
_PERMISSION_FIELDS = frozenset(
    {"scope", "patterns", "description", "required"}
)


class PluginManifestError(ValueError):
    """Base class for stable, machine-classifiable Manifest diagnostics."""

    code = "manifest.invalid"
    category = "schema"

    def __init__(self, message: str, *, path: Optional[Path] = None) -> None:
        self.detail = str(message)
        self.path = str(path) if path is not None else ""
        location = f" ({self.path})" if self.path else ""
        super().__init__(f"[{self.code}] {self.detail}{location}")

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "code": self.code,
            "message": self.detail,
            "path": self.path,
        }


class PluginManifestNotFoundError(PluginManifestError):
    code = "manifest.not_found"


class PluginManifestSchemaError(PluginManifestError):
    code = "manifest.schema"


class PluginManifestApiVersionError(PluginManifestError):
    code = "manifest.api_version"
    category = "version"


class PluginManifestDependencyError(PluginManifestError):
    code = "manifest.dependency"
    category = "dependency"

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        path: Optional[Path] = None,
    ) -> None:
        self.code = f"manifest.dependency.{reason}"
        super().__init__(message, path=path)


class PluginManifestPermissionError(PluginManifestError):
    code = "manifest.permission"
    category = "permission"


class PluginManifestConfigError(PluginManifestError):
    code = "manifest.config"
    category = "config"


class PluginManifestEntryError(PluginManifestError):
    code = "manifest.entry"
    category = "entry"


def _strict_string(
    value: Any,
    *,
    field_name: str,
    path: Optional[Path],
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise PluginManifestSchemaError(
            f"{field_name} must be a string",
            path=path,
        )
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise PluginManifestSchemaError(
            f"{field_name} must not be empty",
            path=path,
        )
    if normalized != value:
        raise PluginManifestSchemaError(
            f"{field_name} must not contain surrounding whitespace",
            path=path,
        )
    return normalized


def _strict_string_list(
    value: Any,
    *,
    field_name: str,
    path: Optional[Path],
    service_keys: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PluginManifestSchemaError(
            f"{field_name} must be an array",
            path=path,
        )
    normalized = tuple(
        _strict_string(
            item,
            field_name=f"{field_name}[{index}]",
            path=path,
        )
        for index, item in enumerate(value)
    )
    if len(set(normalized)) != len(normalized):
        raise PluginManifestSchemaError(
            f"{field_name} must not contain duplicates",
            path=path,
        )
    if service_keys:
        invalid = [item for item in normalized if not _SERVICE_KEY_PATTERN.fullmatch(item)]
        if invalid:
            raise PluginManifestSchemaError(
                f"{field_name} contains invalid ServiceKey values: {', '.join(invalid)}",
                path=path,
            )
    return normalized


def _parse_permissions(
    raw_permissions: Any, *, path: Optional[Path]
) -> tuple[PluginPermission, ...]:
    if not isinstance(raw_permissions, list):
        raise PluginManifestPermissionError(
            "permissions must be an array of permission objects", path=path
        )
    permissions: list[PluginPermission] = []
    permission_scopes: set[str] = set()
    valid_scopes = {item.value for item in PluginPermissionScope}
    for index, raw_permission in enumerate(raw_permissions):
        if not isinstance(raw_permission, dict):
            raise PluginManifestPermissionError(
                f"permissions[{index}] must be an object", path=path
            )
        unknown = sorted(set(raw_permission) - _PERMISSION_FIELDS)
        if unknown:
            raise PluginManifestPermissionError(
                f"permissions[{index}] has unknown fields: " + ", ".join(unknown),
                path=path,
            )
        scope = _strict_string(
            raw_permission.get("scope"),
            field_name=f"permissions[{index}].scope",
            path=path,
        )
        if scope not in valid_scopes:
            raise PluginManifestPermissionError(
                f"unsupported permission scope: {scope}", path=path
            )
        if scope in permission_scopes:
            raise PluginManifestPermissionError(
                f"duplicate permission scope: {scope}", path=path
            )
        permission_scopes.add(scope)
        patterns = _strict_string_list(
            raw_permission.get("patterns"),
            field_name=f"permissions[{index}].patterns",
            path=path,
        )
        required = raw_permission.get("required", True)
        if not isinstance(required, bool):
            raise PluginManifestPermissionError(
                f"permissions[{index}].required must be a boolean", path=path
            )
        permissions.append(
            PluginPermission(
                scope=scope,
                patterns=list(patterns),
                description=_strict_string(
                    raw_permission.get("description", ""),
                    field_name=f"permissions[{index}].description",
                    path=path,
                    allow_empty=True,
                ),
                required=required,
            )
        )
    return tuple(permissions)


def _validate_release_schema(data: dict[str, Any], *, path: Optional[Path]) -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "plugin_sdk"
        / "schema"
        / "plugin-manifest-v2.schema.json"
    )
    try:
        release_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        release_errors = list(Draft202012Validator(release_schema).iter_errors(data))
    except Exception as exc:
        raise PluginManifestSchemaError(
            f"release Manifest schema is unavailable: {exc}", path=path
        ) from exc
    if release_errors:
        selected = best_match(release_errors) or release_errors[0]
        location = ".".join(str(item) for item in selected.absolute_path)
        prefix = f"{location}: " if location else ""
        raise PluginManifestSchemaError(
            f"{prefix}{selected.message}", path=path
        )


def _service_base(service_key: str) -> str:
    return service_key.rsplit("@", 1)[0]


def _relative_package_path(
    plugin_root: Path,
    raw_path: str,
    *,
    field_name: str,
    manifest_path: Path,
    must_be_file: bool = False,
) -> Path:
    value = Path(raw_path)
    if value.is_absolute():
        raise PluginManifestSchemaError(
            f"{field_name} must be relative to the plugin package",
            path=manifest_path,
        )
    if ".." in value.parts:
        raise PluginManifestSchemaError(
            f"{field_name} must not contain '..' path traversal",
            path=manifest_path,
        )
    root = plugin_root.resolve()
    resolved = (root / value).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PluginManifestSchemaError(
            f"{field_name} escapes the plugin package",
            path=manifest_path,
        ) from exc
    if not resolved.exists():
        raise PluginManifestSchemaError(
            f"{field_name} does not exist: {raw_path}",
            path=manifest_path,
        )
    if must_be_file and not resolved.is_file():
        raise PluginManifestSchemaError(
            f"{field_name} must reference a file: {raw_path}",
            path=manifest_path,
        )
    return resolved


@dataclass(frozen=True)
class PluginManifest:
    """保存通过严格校验的 Manifest v2 单轨插件契约。"""

    name: str
    version: str
    api_version: str
    sdk: str
    python: str
    runtime: str
    entry: str
    provides: tuple[str, ...]
    requires: tuple[str, ...]
    optional: tuple[str, ...]
    permissions: tuple[PluginPermission, ...]
    scope: str
    config_schema: Optional[str]
    requested_trust: str
    desktop_runtime: str = "remote"
    description: str = ""
    author: str = ""
    homepage: str = ""
    commands: tuple[str, ...] = field(default_factory=tuple)
    skills: tuple[str, ...] = field(default_factory=tuple)
    agents: tuple[str, ...] = field(default_factory=tuple)
    mcp_servers: tuple[str, ...] = field(default_factory=tuple)
    async_targets: tuple[str, ...] = field(default_factory=tuple)
    workflows: tuple[str, ...] = field(default_factory=tuple)
    contributes: tuple[str, ...] = field(default_factory=tuple)
    package_root: Optional[Path] = field(default=None, repr=False, compare=False)
    manifest_path: Optional[Path] = field(default=None, repr=False, compare=False)
    config_schema_document: Optional[dict[str, Any]] = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def entry_module(self) -> str:
        """返回 Manifest 入口中的相对 Python 文件。"""

        return self.entry.rsplit(":", 1)[0]

    @property
    def entry_symbol(self) -> str:
        """返回 Manifest 入口中的 Python 符号名。"""

        return self.entry.rsplit(":", 1)[1]

    @property
    def entry_path(self) -> Path:
        """解析且再次校验不会逸出插件包的入口路径。"""

        if self.package_root is None or self.manifest_path is None:
            raise PluginManifestEntryError("manifest package root is unavailable")
        return _relative_package_path(
            self.package_root,
            self.entry_module,
            field_name="entry",
            manifest_path=self.manifest_path,
            must_be_file=True,
        )

    def validate_config(self, value: Any) -> dict[str, Any]:
        """使用 Manifest 绑定的 Schema 校验插件配置。"""

        if not isinstance(value, dict):
            raise PluginManifestConfigError(
                "plugin configuration must be an object",
                path=self.manifest_path,
            )
        if self.config_schema_document is None:
            return dict(value)
        validator = Draft202012Validator(self.config_schema_document)
        errors = list(validator.iter_errors(value))
        if errors:
            selected = best_match(errors) or errors[0]
            location = ".".join(str(item) for item in selected.absolute_path)
            prefix = f"config.{location}: " if location else "config: "
            raise PluginManifestConfigError(
                f"{prefix}{selected.message}",
                path=self.manifest_path,
            )
        return dict(value)

    def to_dict(self) -> dict[str, Any]:
        """返回可稳定序列化的 Manifest 字典。"""

        return {
            "name": self.name,
            "version": self.version,
            "apiVersion": self.api_version,
            "sdk": self.sdk,
            "python": self.python,
            "runtime": self.runtime,
            "entry": self.entry,
            "provides": list(self.provides),
            "requires": list(self.requires),
            "optional": list(self.optional),
            "permissions": [item.to_dict() for item in self.permissions],
            "scope": self.scope,
            "configSchema": self.config_schema,
            "requestedTrust": self.requested_trust,
            "desktopRuntime": self.desktop_runtime,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "commands": list(self.commands),
            "skills": list(self.skills),
            "agents": list(self.agents),
            "mcpServers": list(self.mcp_servers),
            "asyncTargets": list(self.async_targets),
            "workflows": list(self.workflows),
            "contributes": list(self.contributes),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        package_root: Optional[Path] = None,
        manifest_path: Optional[Path] = None,
        validate_package: bool = True,
    ) -> "PluginManifest":
        """从原始字典创建并完成 Schema、版本、路径和权限校验。"""

        if not isinstance(data, dict):
            raise PluginManifestSchemaError(
                "plugin.json root must be an object",
                path=manifest_path,
            )
        keys = set(data)
        forbidden = sorted(keys & _FORBIDDEN_TRUST_FIELDS)
        if forbidden:
            raise PluginManifestPermissionError(
                "manifest cannot grant trust or permissions through: "
                + ", ".join(forbidden),
                path=manifest_path,
            )
        missing = sorted(_REQUIRED_FIELDS - keys)
        if missing:
            raise PluginManifestSchemaError(
                "Manifest v2 requires fields: " + ", ".join(missing),
                path=manifest_path,
            )
        unknown = sorted(keys - _REQUIRED_FIELDS - _OPTIONAL_FIELDS)
        if unknown:
            raise PluginManifestSchemaError(
                "unknown Manifest v2 fields: " + ", ".join(unknown),
                path=manifest_path,
            )

        name = _strict_string(data["name"], field_name="name", path=manifest_path)
        if not _NAME_PATTERN.fullmatch(name):
            raise PluginManifestSchemaError(
                "name must use lowercase letters, digits, '.', '_' or '-'",
                path=manifest_path,
            )
        version = _strict_string(
            data["version"],
            field_name="version",
            path=manifest_path,
        )
        if not _VERSION_PATTERN.fullmatch(version):
            raise PluginManifestSchemaError(
                "version must be a semantic version such as 1.2.3",
                path=manifest_path,
            )
        api_version = _strict_string(
            data["apiVersion"],
            field_name="apiVersion",
            path=manifest_path,
        )
        if api_version != PLUGIN_API_VERSION:
            raise PluginManifestApiVersionError(
                f"unsupported Plugin API {api_version!r}; supported: {PLUGIN_API_VERSION}",
                path=manifest_path,
            )
        sdk = _strict_string(data["sdk"], field_name="sdk", path=manifest_path)
        python = _strict_string(
            data["python"], field_name="python", path=manifest_path
        )
        for field_name, specifier, current_version in (
            ("sdk", sdk, PLUGIN_SDK_VERSION),
            (
                "python",
                python,
                f"{sys.version_info.major}.{sys.version_info.minor}.0",
            ),
        ):
            try:
                accepted = Version(current_version) in SpecifierSet(specifier)
            except (InvalidSpecifier, ValueError) as exc:
                raise PluginManifestApiVersionError(
                    f"{field_name} must be a valid PEP 440 version range",
                    path=manifest_path,
                ) from exc
            if not accepted:
                raise PluginManifestApiVersionError(
                    f"{field_name} range {specifier!r} does not include runtime {current_version}",
                    path=manifest_path,
                )
        runtime = _strict_string(
            data["runtime"],
            field_name="runtime",
            path=manifest_path,
        )
        if runtime not in SUPPORTED_PLUGIN_RUNTIMES:
            raise PluginManifestSchemaError(
                f"unsupported runtime {runtime!r}",
                path=manifest_path,
            )
        entry = _strict_string(
            data["entry"],
            field_name="entry",
            path=manifest_path,
        )
        if entry.count(":") != 1:
            raise PluginManifestEntryError(
                "entry must use '<relative-python-file>:<symbol>'",
                path=manifest_path,
            )
        entry_module, entry_symbol = entry.split(":", 1)
        if not entry_module.endswith(".py") or not _ENTRY_SYMBOL_PATTERN.fullmatch(entry_symbol):
            raise PluginManifestEntryError(
                "entry must use '<relative-python-file>:<python-symbol>'",
                path=manifest_path,
            )

        provides = _strict_string_list(
            data["provides"],
            field_name="provides",
            path=manifest_path,
            service_keys=True,
        )
        requires = _strict_string_list(
            data["requires"],
            field_name="requires",
            path=manifest_path,
            service_keys=True,
        )
        optional = _strict_string_list(
            data["optional"],
            field_name="optional",
            path=manifest_path,
            service_keys=True,
        )
        overlap = sorted(set(requires) & set(optional))
        if overlap:
            raise PluginManifestSchemaError(
                "ServiceKey values cannot be both required and optional: "
                + ", ".join(overlap),
                path=manifest_path,
            )

        permissions = _parse_permissions(data["permissions"], path=manifest_path)

        scope = _strict_string(
            data["scope"],
            field_name="scope",
            path=manifest_path,
        )
        if scope not in SUPPORTED_PLUGIN_SCOPES:
            raise PluginManifestSchemaError(
                f"unsupported plugin scope {scope!r}; supported: workspace",
                path=manifest_path,
            )
        requested_trust = _strict_string(
            data["requestedTrust"],
            field_name="requestedTrust",
            path=manifest_path,
        )
        if requested_trust not in SUPPORTED_REQUESTED_TRUST:
            raise PluginManifestPermissionError(
                f"unsupported requestedTrust {requested_trust!r}",
                path=manifest_path,
            )
        desktop_runtime = _strict_string(
            data.get("desktopRuntime", "remote"),
            field_name="desktopRuntime",
            path=manifest_path,
        )
        if desktop_runtime not in {"remote", "trusted-worker"}:
            raise PluginManifestSchemaError(
                "desktopRuntime must be 'remote' or 'trusted-worker'",
                path=manifest_path,
            )

        contributes = _strict_string_list(
            data.get("contributes", []),
            field_name="contributes",
            path=manifest_path,
        )
        unsupported_contributions = sorted(
            set(contributes) - _SUPPORTED_CONTRIBUTIONS
        )
        if unsupported_contributions:
            raise PluginManifestSchemaError(
                "unsupported contributes values: "
                + ", ".join(unsupported_contributions),
                path=manifest_path,
            )

        raw_config_schema = data["configSchema"]
        if raw_config_schema is not None:
            config_schema = _strict_string(
                raw_config_schema,
                field_name="configSchema",
                path=manifest_path,
            )
        else:
            config_schema = None

        manifest = cls(
            name=name,
            version=version,
            api_version=api_version,
            sdk=sdk,
            python=python,
            runtime=runtime,
            entry=entry,
            provides=provides,
            requires=requires,
            optional=optional,
            permissions=permissions,
            scope=scope,
            config_schema=config_schema,
            requested_trust=requested_trust,
            desktop_runtime=desktop_runtime,
            description=_strict_string(
                data.get("description", ""),
                field_name="description",
                path=manifest_path,
                allow_empty=True,
            ),
            author=_strict_string(
                data.get("author", ""),
                field_name="author",
                path=manifest_path,
                allow_empty=True,
            ),
            homepage=_strict_string(
                data.get("homepage", ""),
                field_name="homepage",
                path=manifest_path,
                allow_empty=True,
            ),
            commands=_strict_string_list(
                data.get("commands", []),
                field_name="commands",
                path=manifest_path,
            ),
            skills=_strict_string_list(
                data.get("skills", []),
                field_name="skills",
                path=manifest_path,
            ),
            agents=_strict_string_list(
                data.get("agents", []),
                field_name="agents",
                path=manifest_path,
            ),
            mcp_servers=_strict_string_list(
                data.get("mcpServers", []),
                field_name="mcpServers",
                path=manifest_path,
            ),
            async_targets=_strict_string_list(data.get('asyncTargets', []), field_name='asyncTargets', path=manifest_path),
            workflows=_strict_string_list(
                data.get("workflows", []),
                field_name="workflows",
                path=manifest_path,
            ),
            contributes=contributes,
            package_root=package_root.resolve() if package_root is not None else None,
            manifest_path=manifest_path,
        )
        _validate_release_schema(data, path=manifest_path)
        if validate_package:
            manifest = manifest._validated_package()
        return manifest

    def _validated_package(self) -> "PluginManifest":
        if self.package_root is None or self.manifest_path is None:
            raise PluginManifestSchemaError("plugin package root is required")
        # Resolve every path before importing the entry module. This also blocks
        # symlink and '..' escapes outside the package root.
        self.entry_path
        if self.async_targets:
            from .async_targets import declarations
            declarations(self.package_root, self.async_targets)
        for field_name, entries in (
            ("commands", self.commands),
            ("skills", self.skills),
            ("agents", self.agents),
            ("mcpServers", self.mcp_servers),
            ("asyncTargets", self.async_targets),
            ("workflows", self.workflows),
        ):
            for index, entry in enumerate(entries):
                _relative_package_path(
                    self.package_root,
                    entry,
                    field_name=f"{field_name}[{index}]",
                    manifest_path=self.manifest_path,
                )

        schema_document: Optional[dict[str, Any]] = None
        if self.config_schema is not None:
            schema_path = _relative_package_path(
                self.package_root,
                self.config_schema,
                field_name="configSchema",
                manifest_path=self.manifest_path,
                must_be_file=True,
            )
            try:
                raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PluginManifestConfigError(
                    f"invalid config schema JSON: {exc}",
                    path=schema_path,
                ) from exc
            if not isinstance(raw_schema, dict):
                raise PluginManifestConfigError(
                    "config schema root must be an object",
                    path=schema_path,
                )
            try:
                Draft202012Validator.check_schema(raw_schema)
            except SchemaError as exc:
                raise PluginManifestConfigError(
                    f"invalid Draft 2020-12 config schema: {exc.message}",
                    path=schema_path,
                ) from exc
            schema_document = raw_schema

        return PluginManifest(
            **{
                **self.__dict__,
                "config_schema_document": schema_document,
            }
        )


def load_manifest(plugin_root: Optional[Path]) -> PluginManifest:
    if plugin_root is None:
        raise PluginManifestNotFoundError("plugin package path cannot be resolved")
    root = plugin_root.resolve()
    manifest_path = root / PLUGIN_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise PluginManifestNotFoundError(
            "Manifest v2 plugin.json is required; legacy manifests are not supported",
            path=manifest_path,
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PluginManifestSchemaError(
            f"invalid plugin.json JSON: {exc}",
            path=manifest_path,
        ) from exc
    return PluginManifest.from_dict(
        data,
        package_root=root,
        manifest_path=manifest_path,
    )


def plan_manifest_order(
    manifests: Iterable[PluginManifest],
    *,
    external_providers: Optional[Mapping[str, str]] = None,
) -> tuple[PluginManifest, ...]:
    """Validate providers/dependencies and return a deterministic load order."""

    candidates = tuple(manifests)
    by_name: dict[str, PluginManifest] = {}
    for manifest in candidates:
        if manifest.name in by_name:
            raise PluginManifestDependencyError(
                f"duplicate plugin name: {manifest.name}",
                reason="duplicate_plugin",
                path=manifest.manifest_path,
            )
        by_name[manifest.name] = manifest

    providers = dict(external_providers or {})
    for manifest in candidates:
        for service in manifest.provides:
            owner = providers.get(service)
            if owner is not None and owner != manifest.name:
                raise PluginManifestDependencyError(
                    f"ServiceKey {service!r} has duplicate providers: {owner}, {manifest.name}",
                    reason="duplicate_provider",
                    path=manifest.manifest_path,
                )
            providers[service] = manifest.name

    provided_bases: dict[str, set[str]] = {}
    for service in providers:
        provided_bases.setdefault(_service_base(service), set()).add(service)

    adjacency: dict[str, set[str]] = {name: set() for name in by_name}
    indegree: dict[str, int] = {name: 0 for name in by_name}
    for manifest in candidates:
        for required in manifest.requires:
            owner = providers.get(required)
            if owner is None:
                alternatives = sorted(provided_bases.get(_service_base(required), ()))
                if alternatives:
                    raise PluginManifestDependencyError(
                        f"plugin {manifest.name!r} requires {required!r}, but only "
                        f"incompatible versions are available: {', '.join(alternatives)}",
                        reason="version_conflict",
                        path=manifest.manifest_path,
                    )
                raise PluginManifestDependencyError(
                    f"plugin {manifest.name!r} requires missing ServiceKey {required!r}",
                    reason="missing",
                    path=manifest.manifest_path,
                )
            if owner in by_name and manifest.name not in adjacency[owner]:
                adjacency[owner].add(manifest.name)
                indegree[manifest.name] += 1
        for optional in manifest.optional:
            owner = providers.get(optional)
            if owner in by_name and manifest.name not in adjacency[owner]:
                adjacency[owner].add(manifest.name)
                indegree[manifest.name] += 1

    ready = sorted(name for name, count in indegree.items() if count == 0)
    ordered_names: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered_names.append(current)
        for consumer in sorted(adjacency[current]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)
                ready.sort()

    if len(ordered_names) != len(by_name):
        cycle_members = sorted(name for name, count in indegree.items() if count > 0)
        manifest = by_name[cycle_members[0]]
        raise PluginManifestDependencyError(
            "plugin ServiceKey dependency cycle: " + " -> ".join(cycle_members),
            reason="cycle",
            path=manifest.manifest_path,
        )
    return tuple(by_name[name] for name in ordered_names)
