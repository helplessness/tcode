"""Runtime 模型发现、别名归一化与服务端白名单。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..config.manager import ConfigManager


PREFERRED_MODEL = "deepseek/deepseek-v4-flash"
MODEL_ALIASES = {
    "deepseek-flash": PREFERRED_MODEL,
    "deepseek-v4-flash": PREFERRED_MODEL,
}


class ModelPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeModelSelection:
    runtime_id: str
    model_id: str
    available_models: tuple[str, ...]


def native_model_ids(workdir: str) -> list[str]:
    config = ConfigManager.load(workdir)
    providers = config.get("provider") if isinstance(config, dict) else {}
    result: list[str] = []
    if isinstance(providers, dict):
        for provider_id, provider_config in providers.items():
            models = (
                provider_config.get("models")
                if isinstance(provider_config, dict)
                else {}
            )
            if not isinstance(models, dict):
                continue
            result.extend(
                f"{provider_id}/{model_id}" for model_id in models if model_id
            )
    return sorted(set(result))


def configured_native_model(workdir: str) -> str:
    config = ConfigManager.load(workdir)
    return str(config.get("model") or "").strip() if isinstance(config, dict) else ""


def _allowlist(runtime_id: str) -> set[str] | None:
    """读取 `runtime=model|model;runtime=model` 形式的部署白名单。"""

    raw = str(os.environ.get("TCODE_RUNTIME_MODEL_ALLOWLIST") or "").strip()
    if not raw:
        return None
    for entry in raw.split(";"):
        key, separator, values = entry.partition("=")
        if separator and key.strip() == runtime_id:
            return {
                MODEL_ALIASES.get(item.strip(), item.strip())
                for item in values.split("|")
                if item.strip()
            }
    return set()


def normalize_model_alias(model_id: str) -> str:
    value = str(model_id or "").strip()
    return MODEL_ALIASES.get(value, value)


def select_runtime_model(
    runtime_id: str,
    requested_model: str,
    available_models: list[str] | tuple[str, ...],
    *,
    default_model: str = "",
) -> RuntimeModelSelection:
    available = tuple(dict.fromkeys(str(item).strip() for item in available_models if str(item).strip()))
    requested = normalize_model_alias(requested_model)
    default = normalize_model_alias(default_model)
    selected = requested or (PREFERRED_MODEL if PREFERRED_MODEL in available else default)
    if not selected and available:
        selected = available[0]
    if selected and available and selected not in available:
        raise ModelPolicyError(
            f"model is unavailable for runtime {runtime_id}: {selected}"
        )
    allowlist = _allowlist(runtime_id)
    if selected and allowlist is not None and selected not in allowlist:
        raise ModelPolicyError(
            f"model is not allowed for runtime {runtime_id}: {selected}"
        )
    return RuntimeModelSelection(runtime_id, selected, available)


async def resolve_runtime_model(
    runtime_id: str,
    requested_model: str,
    *,
    workdir: str,
    runtime_registry: Any,
    backend_registry: Any | None = None,
) -> RuntimeModelSelection:
    if runtime_id == "tcode-native":
        models = native_model_ids(workdir)
        return select_runtime_model(
            runtime_id,
            requested_model,
            models,
            default_model=configured_native_model(workdir),
        )
    if runtime_id == "direct-echo":
        return select_runtime_model(runtime_id, "", ())
    definition = runtime_registry.get(runtime_id)
    if definition is None:
        backend = backend_registry.get(runtime_id) if backend_registry is not None else None
        if backend is None:
            raise ModelPolicyError(f"runtime is not registered: {runtime_id}")
        capabilities = await backend.get_capabilities()
        models = capabilities.get("models") if isinstance(capabilities, dict) else []
        default_model = (
            str(capabilities.get("defaultModel") or "")
            if isinstance(capabilities, dict)
            else ""
        )
        return select_runtime_model(
            runtime_id,
            requested_model,
            models if isinstance(models, (list, tuple)) else [],
            default_model=default_model,
        )
    probe = next(
        (
            item
            for item in await runtime_registry.probes(include_models=True)
            if item.runtime_id == runtime_id
        ),
        None,
    )
    if probe is None or not probe.available:
        raise ModelPolicyError(f"runtime is unavailable: {runtime_id}")
    return select_runtime_model(runtime_id, requested_model, probe.models)


__all__ = [
    "MODEL_ALIASES",
    "ModelPolicyError",
    "PREFERRED_MODEL",
    "RuntimeModelSelection",
    "configured_native_model",
    "native_model_ids",
    "normalize_model_alias",
    "resolve_runtime_model",
    "select_runtime_model",
]
