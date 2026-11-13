"""Instance Web model catalog. Credentials never enter merged/task configuration."""

import json
import os
import re
from pathlib import Path

from .atomic_storage import atomic_write_json, file_transaction
from .vision import model_config_supports_images

PREFIX = "web-model-"


def settings_path() -> Path:
    root = Path(os.environ.get("TCODE_WORKDIR") or os.getcwd()).resolve()
    path = root / ".tcode" / "web-models.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError("Invalid model settings path")
    return path


def read_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("services", {}), dict):
        raise ValueError("Invalid model settings")
    for sid, service in data.get("services", {}).items():
        if (not re.fullmatch(r"[a-f0-9]{32}", sid) or not isinstance(service, dict)
                or not all(isinstance(service.get(field), str) for field in ("name", "protocol", "base_url", "revision"))
                or service["protocol"] not in {"openai_compatible", "ollama", "anthropic"}
                or not isinstance(service.get("api_key", ""), str)
                or not isinstance(service.get("models"), list)
                or not all(isinstance(model, str) for model in service["models"])
                or not isinstance(service.get("vision_verified", []), list)
                or not isinstance(service.get("results", {}), dict)):
            raise ValueError("Invalid model service")
    return data


def update_settings(change) -> dict:
    path = settings_path()
    with file_transaction(path):
        data = read_settings()
        change(data)
        atomic_write_json(path, data)
    return data


def apply_settings(config: dict) -> None:
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        return
    try:
        settings = read_settings()
    except (OSError, ValueError):
        return  # A damaged optional store must not prevent file-configured chat.
    if not settings:
        return
    providers = config.get("provider")
    if not isinstance(providers, dict):
        providers = config["provider"] = {}
    active = set()
    for sid, service in settings.get("services", {}).items():
        provider_id = PREFIX + sid
        if provider_id in providers:
            continue  # Deployment files own any explicit collision.
        active.add(provider_id)
        providers[provider_id] = {
            "name": service["name"],
            "npm": "@ai-sdk/anthropic" if service["protocol"] == "anthropic" else "@ai-sdk/openai-compatible",
            "options": {"baseURL": service["base_url"], "apiMode": "chat_completions", "webModelService": sid},
            "models": {
                model: {"name": model, **({"supports_images": True} if model in service.get("vision_verified", []) else {})}
                for model in service["models"]
            },
        }
    for name in ("model", "vision_model"):
        ref = settings.get(name, "")
        if not isinstance(ref, str):
            continue
        provider_id, _, model = ref.partition("/")
        provider = providers.get(provider_id, {})
        if provider_id.startswith(PREFIX) and provider_id not in active:
            continue
        if isinstance(provider, dict) and isinstance(provider.get("models"), dict) and model in provider["models"]:
            if name == "vision_model" and model_config_supports_images(provider["models"][model]) is not True:
                continue
            config[name] = ref


def service_credential(provider_id: str, sid: str, base_url: str, protocol: str) -> str:
    """A frozen run can never send a saved key to a changed endpoint."""
    if provider_id != PREFIX + sid or os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        raise ValueError("Invalid Web model service")
    service = read_settings().get("services", {}).get(sid)
    if not service or (service["base_url"], service["protocol"] == "anthropic") != (base_url, protocol == "anthropic"):
        raise ValueError("模型服务已变更，请在新会话中选择当前配置")
    return service.get("api_key", "")
