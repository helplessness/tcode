"""Shared helpers for routing image inputs across model capabilities."""

from __future__ import annotations

from typing import Any, Optional


def model_config_supports_images(model_cfg: Any) -> Optional[bool]:
    """Return an explicit image capability, or ``None`` when unspecified."""
    if not isinstance(model_cfg, dict):
        return None
    direct = model_cfg.get("supports_images")
    if isinstance(direct, bool):
        return direct
    capabilities = model_cfg.get("capabilities")
    if isinstance(capabilities, dict):
        for key in ("vision", "images", "image_input"):
            if isinstance(capabilities.get(key), bool):
                return bool(capabilities[key])
    for key in ("input_modalities", "modalities"):
        modalities = model_cfg.get(key)
        if isinstance(modalities, (list, tuple, set)) and modalities:
            normalized = {str(item or "").strip().lower() for item in modalities}
            return bool(normalized & {"image", "images", "vision"})
    return None


def configured_model_supports_images(
    config: dict[str, Any],
    model_ref: str,
) -> Optional[bool]:
    """Resolve the declared image capability for ``provider/model``."""
    normalized = str(model_ref or config.get("model") or "").strip()
    if "/" not in normalized:
        return None
    provider_id, model_id = normalized.split("/", 1)
    providers = config.get("provider")
    provider_cfg = providers.get(provider_id) if isinstance(providers, dict) else None
    models = provider_cfg.get("models") if isinstance(provider_cfg, dict) else None
    model_cfg = models.get(model_id) if isinstance(models, dict) else None
    return model_config_supports_images(model_cfg)


def select_vision_model_ref(
    config: dict[str, Any],
    *,
    exclude: str = "",
) -> Optional[str]:
    """Choose a model explicitly configured for image input.

    An explicit top-level/runtime ``vision_model`` wins. Otherwise the first
    model declaring image support is used. Unknown models are intentionally not
    selected for the bridge because doing so would merely repeat the failed
    capability probe.
    """
    excluded = str(exclude or "").strip()
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    preferred = str(
        config.get("vision_model")
        or runtime.get("vision_model")
        or runtime.get("visionModel")
        or ""
    ).strip()
    if preferred and preferred != excluded and "/" in preferred:
        provider_id, _model_id = preferred.split("/", 1)
        providers = config.get("provider")
        provider_cfg = (
            providers.get(provider_id) if isinstance(providers, dict) else None
        )
        declared_support = configured_model_supports_images(config, preferred)
        # The explicit bridge model is also useful for provider model IDs that
        # are deployed before the local model catalog is refreshed.  Honor it
        # whenever its provider exists, unless the model is explicitly marked
        # as text-only.
        if isinstance(provider_cfg, dict) and declared_support is not False:
            return preferred

    providers = config.get("provider")
    if not isinstance(providers, dict):
        return None
    for provider_id, provider_cfg in providers.items():
        models = provider_cfg.get("models") if isinstance(provider_cfg, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, model_cfg in models.items():
            model_ref = f"{provider_id}/{model_id}"
            if model_ref == excluded:
                continue
            if model_config_supports_images(model_cfg) is True:
                return model_ref
    return None
