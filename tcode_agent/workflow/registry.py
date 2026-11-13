"""Registry for declarative workflow templates."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from ..runtime.registrations import RegistrationDisposer, RegistrationToken
from .models import WorkflowTemplate
from .input_bindings import request_binding_field


from ..runtime.workspace_registry import WorkspaceRegistryMeta


class WorkflowTemplateRegistry(metaclass=WorkspaceRegistryMeta):

    """统一管理内建模板和带插件实例所有权的声明式模板。"""
    _workspace_fields = ('_plugin_templates', '_plugin_registrations')

    _cache: dict[str, WorkflowTemplate] = {}
    _plugin_templates: dict[str, WorkflowTemplate] = {}
    _plugin_registrations: dict[str, RegistrationToken] = {}
    _TEMPLATE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    _PLUGIN_LOCAL_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    _WORKFLOW_JSON_NAME = "workflow.json"
    _DEFAULT_PROMPT_DIR = "prompts"
    _PROMPT_FILE_MAX_CHARS = 120_000
    _PLUGIN_WORKFLOW_JSON_MAX_BYTES = 512 * 1024
    _PLUGIN_WORKFLOW_MAX_COUNT = 32
    _PLUGIN_WORKFLOW_MAX_STAGES = 32
    _PLUGIN_WORKFLOW_CALL_TYPES = frozenset({"agent", "subagent"})

    @classmethod
    def template_root(cls) -> Path:
        """Return the template directory for workflow JSON files."""

        return Path(__file__).resolve().parent / "templates"

    @classmethod
    def _template_paths(cls) -> list[Path]:
        root = cls.template_root()
        if not root.exists():
            return []
        flat_paths = [path for path in root.glob("*.json") if path.is_file()]
        folder_paths = [
            path / cls._WORKFLOW_JSON_NAME
            for path in root.iterdir()
            if path.is_dir() and (path / cls._WORKFLOW_JSON_NAME).is_file()
        ]
        return sorted(
            [*flat_paths, *folder_paths],
            key=lambda path: (
                cls._template_path_key(path),
                1 if cls._is_folder_template_path(path) else 0,
                str(path),
            ),
        )

    @classmethod
    def _is_folder_template_path(cls, path: Path) -> bool:
        return path.name == cls._WORKFLOW_JSON_NAME and path.parent != cls.template_root()

    @classmethod
    def _template_path_key(cls, path: Path) -> str:
        if cls._is_folder_template_path(path):
            return path.parent.name
        return path.stem

    @classmethod
    def reload(cls) -> None:
        """清除内建文件缓存，但保留仍处于活动生命周期的插件模板。"""

        cls._cache = {}

    @classmethod
    def clear_plugin_templates(cls) -> None:
        """清空全部插件模板注册，仅供进程关闭和测试隔离使用。"""

        cls._plugin_templates = {}
        cls._plugin_registrations = {}

    @classmethod
    def _builtin_templates(cls) -> dict[str, WorkflowTemplate]:
        """读取并按模板键返回当前全部内建模板。"""

        templates: dict[str, WorkflowTemplate] = {}
        for path in cls._template_paths():
            template = cls.load_from_path(path)
            if template is not None:
                templates[template.template_key] = template
        return templates

    @classmethod
    def _normalize_template_key(cls, template_key: str) -> str:
        normalized = str(template_key or "").strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or not cls._TEMPLATE_KEY_PATTERN.fullmatch(normalized)
        ):
            raise ValueError("invalid workflow template_key")
        return normalized

    @classmethod
    def template_path(cls, template_key: str) -> Path:
        """Return the canonical JSON file path for one template key."""

        normalized = cls._normalize_template_key(template_key)
        root = cls.template_root().resolve()
        folder_path = (root / normalized / cls._WORKFLOW_JSON_NAME).resolve()
        path = folder_path if folder_path.exists() else (root / f"{normalized}.json").resolve()
        try:
            path.relative_to(root)
        except Exception:
            raise ValueError("workflow template path escapes template root")
        return path

    @classmethod
    def list_templates(cls, *, include_disabled: bool = False) -> list[WorkflowTemplate]:
        """合并内建和插件来源，并返回稳定排序的可见模板。"""

        by_key = cls._builtin_templates()
        by_key.update(cls._plugin_templates)
        return [
            template
            for template in sorted(by_key.values(), key=lambda item: item.template_key)
            if include_disabled or template.enabled
        ]

    @classmethod
    def get(
        cls,
        template_key: str,
        *,
        include_disabled: bool = False,
    ) -> Optional[WorkflowTemplate]:
        """按完整模板键读取内建或插件模板。"""

        normalized = str(template_key or "").strip()
        if not normalized:
            return None
        plugin_template = cls._plugin_templates.get(normalized)
        if plugin_template is not None:
            if plugin_template.enabled or include_disabled:
                return plugin_template
            return None
        matched: Optional[WorkflowTemplate] = None
        for path in cls._template_paths():
            template = cls.load_from_path(path)
            if template is not None and template.template_key == normalized:
                matched = template
        if matched is not None:
            if matched.enabled or include_disabled:
                return matched
            return None
        cls._cache.pop(normalized, None)
        return None

    @classmethod
    def load_from_path(cls, path: Path) -> Optional[WorkflowTemplate]:
        """宽容加载一个内建模板，格式错误时返回空值。"""

        try:
            template = cls.load_from_path_strict(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        cls._cache[template.template_key] = template
        return template

    @classmethod
    def load_from_path_strict(cls, path: Path) -> WorkflowTemplate:
        """严格加载单个模板，保留可分类的解析与合同异常。"""

        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("workflow template payload must be a JSON object")
        if cls._is_folder_template_path(path):
            payload = cls._apply_folder_prompt_files(payload, path.parent)
        return cls.validate_payload(payload)

    @classmethod
    def _plugin_workflow_paths(
        cls,
        plugin_root: Path,
        entries: tuple[str, ...],
    ) -> list[Path]:
        """发现 Manifest 声明目录中的模板文件并阻止路径逃逸。"""

        root = plugin_root.resolve()
        discovered: dict[str, Path] = {}
        for index, entry in enumerate(entries):
            declared = (root / entry).resolve()
            try:
                declared.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"workflows[{index}] escapes plugin package") from exc
            candidates: list[Path]
            if declared.is_file():
                candidates = [declared]
            elif declared.is_dir():
                candidates = [
                    path
                    for path in declared.rglob(cls._WORKFLOW_JSON_NAME)
                    if path.is_file()
                ]
                candidates.extend(
                    path
                    for path in declared.glob("*.json")
                    if path.is_file() and path.name != cls._WORKFLOW_JSON_NAME
                )
            else:
                raise ValueError(f"workflows[{index}] does not exist: {entry}")
            for candidate in candidates:
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(root).as_posix()
                except ValueError as exc:
                    raise ValueError(
                        f"plugin workflow path escapes package: {candidate}"
                    ) from exc
                discovered[relative] = resolved
        paths = [discovered[key] for key in sorted(discovered)]
        if not paths:
            raise ValueError("plugin workflows declaration contains no workflow templates")
        if len(paths) > cls._PLUGIN_WORKFLOW_MAX_COUNT:
            raise ValueError(
                f"plugin workflow count exceeds {cls._PLUGIN_WORKFLOW_MAX_COUNT}"
            )
        return paths

    @classmethod
    def load_plugin_templates(
        cls,
        plugin_root: Path,
        entries: tuple[str, ...],
        *,
        source: str,
    ) -> tuple[WorkflowTemplate, ...]:
        """预加载、命名空间化并收窄插件声明式 Workflow。"""

        if not entries:
            return ()
        templates: list[WorkflowTemplate] = []
        local_keys: set[str] = set()
        root = plugin_root.resolve()
        for path in cls._plugin_workflow_paths(root, entries):
            if path.stat().st_size > cls._PLUGIN_WORKFLOW_JSON_MAX_BYTES:
                raise ValueError(f"plugin workflow JSON is too large: {path.name}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("plugin workflow payload must be a JSON object")
            payload = cls._apply_folder_prompt_files(payload, path.parent)
            template = cls.validate_payload(payload)
            local_key = str(template.template_key or "").strip()
            if not cls._PLUGIN_LOCAL_KEY_PATTERN.fullmatch(local_key):
                raise ValueError(
                    "plugin workflow template_key must use lowercase letters, "
                    "digits, '_' or '-' and be at most 64 characters"
                )
            if local_key in local_keys:
                raise ValueError(f"duplicate plugin workflow template_key: {local_key}")
            local_keys.add(local_key)
            namespaced_key = f"{source}.{local_key}"
            cls._normalize_template_key(namespaced_key)
            if len(template.stages) > cls._PLUGIN_WORKFLOW_MAX_STAGES:
                raise ValueError(
                    f"plugin workflow stage count exceeds {cls._PLUGIN_WORKFLOW_MAX_STAGES}: "
                    f"{local_key}"
                )
            for stage in template.stages:
                call_type = str(stage.call_type or "").strip().lower()
                if call_type not in cls._PLUGIN_WORKFLOW_CALL_TYPES:
                    raise ValueError(
                        f"plugin workflow call_type is not allowed: "
                        f"{local_key}.{stage.stage_id} -> {call_type or '(empty)'}"
                    )
                if bool((stage.metadata or {}).get("prompt_truncated")):
                    raise ValueError(
                        f"plugin workflow prompt exceeds size limit: "
                        f"{local_key}.{stage.stage_id}"
                    )
            payload = template.to_dict(include_prompt_content=True)
            metadata = dict(payload.get("metadata") or {})
            routing = dict(metadata.get("routing") or {})
            routing["auto_start"] = False
            metadata.update(
                {
                    "routing": routing,
                    "plugin_source": source,
                    "plugin_local_template_key": local_key,
                    "plugin_source_path": path.relative_to(root).as_posix(),
                    "requires_explicit_confirmation": True,
                }
            )
            payload["template_key"] = namespaced_key
            payload["metadata"] = metadata
            templates.append(cls.validate_payload(payload))
        return tuple(templates)

    @classmethod
    def register_plugin_template(
        cls,
        template: WorkflowTemplate,
        *,
        source: str,
        instance_id: str,
        replace: bool = False,
    ) -> RegistrationDisposer:
        """登记一个插件模板并返回精确实例卸载句柄。"""

        key = cls._normalize_template_key(template.template_key)
        if key in cls._builtin_templates():
            raise ValueError(f"plugin workflow conflicts with builtin template: {key}")
        if not replace and key in cls._plugin_templates:
            raise ValueError(f"plugin workflow is already registered: {key}")
        payload = template.to_dict(include_prompt_content=True)
        metadata = dict(payload.get("metadata") or {})
        metadata["plugin_instance_id"] = str(instance_id or "")
        payload["metadata"] = metadata
        registered = cls.validate_payload(payload)
        token = RegistrationToken.create(
            registry="workflow-templates",
            key=key,
            source=source,
            instance_id=instance_id,
        )
        cls._plugin_templates[key] = registered
        cls._plugin_registrations[key] = token
        return RegistrationDisposer(token, cls.unregister_exact)

    @classmethod
    def unregister_exact(cls, token: RegistrationToken) -> bool:
        """仅在注册令牌完全一致时移除对应插件模板。"""

        current = cls._plugin_registrations.get(token.key)
        if current != token:
            return False
        cls._plugin_registrations.pop(token.key, None)
        cls._plugin_templates.pop(token.key, None)
        return True

    @classmethod
    def unregister_by_instance(cls, instance_id: str) -> list[str]:
        """移除指定插件实例拥有的全部 Workflow 模板。"""

        removed: list[str] = []
        for key, token in list(cls._plugin_registrations.items()):
            if token.instance_id == instance_id and cls.unregister_exact(token):
                removed.append(key)
        return sorted(removed)

    @classmethod
    def unregister_by_source(
        cls,
        source: str,
        *,
        legacy_only: bool = False,
    ) -> list[str]:
        """按来源清理模板，并可只处理没有实例身份的旧注册。"""

        removed: list[str] = []
        for key, token in list(cls._plugin_registrations.items()):
            if token.source != source or (legacy_only and token.instance_id):
                continue
            if cls.unregister_exact(token):
                removed.append(key)
        return sorted(removed)

    @classmethod
    def _apply_folder_prompt_files(
        cls,
        payload: dict[str, Any],
        template_dir: Path,
    ) -> dict[str, Any]:
        """解析模板阶段引用的 Markdown Prompt，并记录缺失或截断状态。"""

        data = dict(payload or {})
        metadata = dict(data.get("metadata", {}) or {})
        prompt_dir = str(metadata.get("prompt_dir") or cls._DEFAULT_PROMPT_DIR).strip()
        if not prompt_dir:
            prompt_dir = cls._DEFAULT_PROMPT_DIR
        metadata.setdefault("template_layout", "directory")
        metadata.setdefault("prompt_dir", prompt_dir)

        stages: list[Any] = []
        for item in data.get("stages", []) or []:
            if not isinstance(item, dict):
                stages.append(item)
                continue
            stage = dict(item)
            stage_metadata = dict(stage.get("metadata", {}) or {})
            prompt_ref = (
                stage.get("prompt_file")
                or stage_metadata.get("prompt_file")
                or ""
            )
            explicit_prompt_ref = bool(str(prompt_ref or "").strip())
            if not explicit_prompt_ref:
                stage_id = str(stage.get("stage_id", "") or "").strip()
                if stage_id:
                    prompt_ref = f"{prompt_dir}/{stage_id}.md"
            prompt_payload = cls._load_stage_prompt_file(
                template_dir,
                prompt_ref,
                default_prompt_dir=prompt_dir,
            )
            if prompt_payload:
                stage_metadata.setdefault("prompt_file", prompt_payload["source"])
                stage_metadata["prompt_source"] = prompt_payload["source"]
                stage_metadata["prompt_title"] = prompt_payload.get("title", "")
                stage_metadata["prompt_markdown"] = prompt_payload.get("markdown", "")
                stage_metadata["prompt_sections"] = prompt_payload.get("sections", {})
                stage_metadata["prompt_truncated"] = bool(
                    prompt_payload.get("truncated")
                )
            elif explicit_prompt_ref:
                normalized_ref = cls._normalize_prompt_ref(
                    prompt_ref,
                    default_prompt_dir=prompt_dir,
                )
                if normalized_ref:
                    stage_metadata["prompt_file"] = normalized_ref
                    stage_metadata["prompt_missing"] = True
            if stage_metadata:
                stage["metadata"] = stage_metadata
            stages.append(stage)

        data["metadata"] = metadata
        data["stages"] = stages
        return data

    @classmethod
    def _normalize_prompt_ref(
        cls,
        prompt_ref: Any,
        *,
        default_prompt_dir: str,
    ) -> str:
        raw = str(prompt_ref or "").strip()
        if not raw:
            return ""
        raw_path = Path(raw)
        if raw_path.is_absolute():
            return ""
        parts = raw_path.parts
        if any(part in {"", ".", ".."} for part in parts):
            return ""
        if raw_path.suffix.lower() not in {".md", ".markdown"}:
            return ""
        if len(parts) == 1:
            raw_path = Path(default_prompt_dir) / raw_path
        return raw_path.as_posix()

    @classmethod
    def _load_stage_prompt_file(
        cls,
        template_dir: Path,
        prompt_ref: Any,
        *,
        default_prompt_dir: str,
    ) -> dict[str, Any]:
        normalized_ref = cls._normalize_prompt_ref(
            prompt_ref,
            default_prompt_dir=default_prompt_dir,
        )
        if not normalized_ref:
            return {}
        root = template_dir.resolve()
        path = (root / normalized_ref).resolve()
        try:
            path.relative_to(root)
        except Exception:
            return {}
        if not path.is_file():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return {}
        truncated = False
        if len(text) > cls._PROMPT_FILE_MAX_CHARS:
            text = text[: cls._PROMPT_FILE_MAX_CHARS]
            truncated = True
        parsed = cls._parse_prompt_markdown(text)
        parsed["source"] = normalized_ref
        parsed["truncated"] = truncated
        return parsed

    @classmethod
    def _parse_prompt_markdown(cls, text: str) -> dict[str, Any]:
        content = str(text or "").strip()
        title = ""
        sections: dict[str, str] = {}
        current_key = "body"
        buffers: dict[str, list[str]] = {current_key: []}

        for line in content.splitlines():
            h1 = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
            if h1 and not title:
                title = h1.group(1).strip()
                continue
            h2 = re.match(r"^##\s+(.+?)\s*#*\s*$", line)
            if h2:
                current_key = cls._prompt_section_key(h2.group(1))
                buffers.setdefault(current_key, [])
                continue
            buffers.setdefault(current_key, []).append(line)

        for key, lines in buffers.items():
            value = "\n".join(lines).strip()
            if value:
                sections[key] = value
        return {
            "title": title,
            "markdown": content,
            "sections": sections,
        }

    @classmethod
    def _prompt_section_key(cls, heading: str) -> str:
        key = str(heading or "").strip().lower()
        key = re.sub(r"`+", "", key)
        key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", key)
        key = key.strip("_")
        return key or "section"

    @classmethod
    def validate_payload(cls, payload: dict) -> WorkflowTemplate:
        from .diagnostics import configuration_error_text
        try:
            return cls._validate_payload(payload)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(configuration_error_text(exc)) from exc

    @classmethod
    def _validate_payload(cls, payload: dict) -> WorkflowTemplate:
        """Validate a raw template payload and return its normalized template."""

        if not isinstance(payload, dict):
            raise ValueError("workflow template payload must be a JSON object")
        if not isinstance(payload.get("stages"), list):
            raise ValueError("stages 必须是阶段数组。")
        for index, raw_stage in enumerate(payload["stages"]):
            if not isinstance(raw_stage, dict):
                raise ValueError(f"stages[{index}] 必须是阶段对象，不能忽略该项。")
            for field in ("depends_on", "allowed_tools", "required_skills", "validators", "expected_artifacts", "fallback_stage_ids"):
                if field in raw_stage and not isinstance(raw_stage[field], list):
                    raise ValueError(f"stages[{index}].{field} 必须是数组。")
            for field in ("input_contract", "output_contract", "metadata", "retry_policy", "timeout_policy", "failure_policy"):
                if field in raw_stage and not isinstance(raw_stage[field], dict):
                    raise ValueError(f"stages[{index}].{field} 必须是对象。")
            if any(not isinstance(item, dict) for item in raw_stage.get("validators", [])):
                raise ValueError(f"stages[{index}].validators 每项必须是校验规则对象。")
        request_binding_field(payload.get("input_contract"))
        template = WorkflowTemplate.from_dict(payload)
        cls._normalize_template_key(template.template_key)
        if not template.display_name:
            raise ValueError("workflow template display_name is required")
        if not template.domain:
            raise ValueError("workflow template domain is required")
        if not template.stages:
            raise ValueError("workflow template must include at least one stage")

        raw_stages = [
            item for item in payload.get("stages", []) or [] if isinstance(item, dict)
        ]
        for raw_stage in raw_stages:
            if "depends_on" not in raw_stage:
                raise ValueError(
                    "workflow stage depends_on is required in the new template format: "
                    + str(raw_stage.get("stage_id") or "(unknown)")
                )

        seen_stage_ids: set[str] = set()
        for stage in template.stages:
            stage_id = str(stage.stage_id or "").strip()
            if not stage_id:
                raise ValueError("workflow stage_id is required")
            if stage_id in seen_stage_ids:
                raise ValueError(f"duplicate workflow stage_id: {stage_id}")
            seen_stage_ids.add(stage_id)
            if not stage.label:
                raise ValueError(f"workflow stage label is required: {stage_id}")
            if not stage.owner:
                raise ValueError(f"workflow stage owner is required: {stage_id}")
            if not stage.call_type:
                raise ValueError(f"workflow stage call_type is required: {stage_id}")
            if bool((stage.metadata or {}).get("prompt_missing")):
                raise ValueError(f"workflow stage prompt file is missing: {stage_id}")

            from .validation import SUPPORTED_STAGE_VALIDATORS

            for validator in stage.validators:
                validator_type = str(validator.get("type") or "").strip().lower()
                if validator_type not in SUPPORTED_STAGE_VALIDATORS:
                    raise ValueError(
                        f"unsupported workflow validator: {stage_id} -> {validator_type or '(empty)'}"
                    )
                if validator_type in {"expected_artifact_coverage", "required_artifacts", "files_exist"}:
                    names = validator.get("names")
                    if names is not None and not isinstance(names, list):
                        raise ValueError(
                            f"workflow validator names must be an array: {stage_id} -> {validator_type}"
                        )
                if validator_type in {"contains_all", "contains_any"}:
                    terms = validator.get("terms")
                    if not isinstance(terms, list) or not any(
                        str(item or "").strip() for item in terms
                    ):
                        raise ValueError(
                            f"workflow validator terms must be a non-empty array: {stage_id} -> {validator_type}"
                        )
                if validator_type == "json_schema":
                    schema = validator.get("schema")
                    if schema is not None and not isinstance(schema, dict):
                        raise ValueError(
                            f"workflow json_schema validator schema must be an object: {stage_id}"
                        )
            if stage.retry_policy:
                try:
                    max_attempts = int(stage.retry_policy.get("max_attempts", 1) or 1)
                except Exception as exc:
                    raise ValueError(
                        f"invalid workflow retry max_attempts: {stage_id}"
                    ) from exc
                if max_attempts < 1 or max_attempts > 20:
                    raise ValueError(
                        f"workflow retry max_attempts must be between 1 and 20: {stage_id}"
                    )
            if stage.timeout_policy:
                try:
                    timeout_seconds = float(
                        stage.timeout_policy.get("seconds")
                        or stage.timeout_policy.get("timeout_seconds")
                        or 0
                    )
                except Exception as exc:
                    raise ValueError(f"invalid workflow timeout: {stage_id}") from exc
                if timeout_seconds < 30 or timeout_seconds > 24 * 60 * 60:
                    raise ValueError(
                        f"workflow timeout must be between 30 and 86400 seconds: {stage_id}"
                    )
                on_timeout = str(stage.timeout_policy.get("on_timeout") or "retry").strip().lower()
                if on_timeout not in {"retry", "fail", "block"}:
                    raise ValueError(
                        f"unsupported workflow timeout action: {stage_id} -> {on_timeout}"
                    )
            failure_action = str(
                (stage.failure_policy or {}).get("action") or ""
            ).strip().lower()
            if failure_action and failure_action not in {"block", "fallback"}:
                raise ValueError(
                    f"unsupported workflow failure action: {stage_id} -> {failure_action}"
                )
            fallback_on_codes = (stage.failure_policy or {}).get("fallback_on_codes")
            if fallback_on_codes is not None and (
                not isinstance(fallback_on_codes, list)
                or not fallback_on_codes
                or any(not str(item or "").strip() for item in fallback_on_codes)
            ):
                raise ValueError(
                    f"workflow fallback_on_codes must be a non-empty string array: {stage_id}"
                )
            if fallback_on_codes is not None and failure_action != "fallback":
                raise ValueError(
                    f"workflow fallback_on_codes requires fallback action: {stage_id}"
                )

        for stage in template.stages:
            for dependency_stage_id in stage.depends_on:
                if dependency_stage_id not in seen_stage_ids:
                    raise ValueError(
                        f"dependency stage not found: {stage.stage_id} -> {dependency_stage_id}"
                    )
                if dependency_stage_id == stage.stage_id:
                    raise ValueError(f"workflow stage cannot depend on itself: {stage.stage_id}")
            for fallback_stage_id in stage.fallback_stage_ids:
                if fallback_stage_id not in seen_stage_ids:
                    raise ValueError(
                        f"fallback stage not found: {stage.stage_id} -> {fallback_stage_id}"
                    )
                if fallback_stage_id == stage.stage_id:
                    raise ValueError(
                        f"workflow stage cannot fallback to itself: {stage.stage_id}"
                    )

        dependency_graph = {
            stage.stage_id: list(stage.depends_on)
            for stage in template.stages
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def _visit(stage_id: str) -> None:
            if stage_id in visiting:
                raise ValueError(f"workflow dependency cycle detected at stage: {stage_id}")
            if stage_id in visited:
                return
            visiting.add(stage_id)
            for dependency in dependency_graph.get(stage_id, []):
                _visit(dependency)
            visiting.remove(stage_id)
            visited.add(stage_id)

        for stage_id in dependency_graph:
            _visit(stage_id)
        if not any(not dependencies for dependencies in dependency_graph.values()):
            raise ValueError("workflow template must include at least one root stage")
        return template

    @classmethod
    def save_template(cls, payload: dict, *, overwrite: bool = False) -> WorkflowTemplate:
        """校验并持久化一个宿主内建 Workflow 模板。"""

        template = cls.validate_payload(payload)
        if template.template_key in cls._plugin_templates:
            raise FileExistsError(
                f"workflow template is owned by an active plugin: {template.template_key}"
            )
        path = cls.template_path(template.template_key)
        if path.exists() and not overwrite:
            raise FileExistsError(f"workflow template already exists: {template.template_key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(template.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        cls._cache[template.template_key] = template
        return template

    @classmethod
    def delete_template(cls, template_key: str) -> bool:
        """Delete one workflow template JSON file if it exists."""

        normalized = cls._normalize_template_key(template_key)
        root = cls.template_root().resolve()
        flat_path = (root / f"{normalized}.json").resolve()
        folder_path = (root / normalized / cls._WORKFLOW_JSON_NAME).resolve()
        deleted = False
        for path in (flat_path, folder_path):
            try:
                path.relative_to(root)
            except Exception:
                continue
            if not path.exists():
                continue
            if cls._is_folder_template_path(path):
                shutil.rmtree(path.parent)
            else:
                path.unlink()
            deleted = True
        cls._cache.pop(normalized, None)
        return deleted
