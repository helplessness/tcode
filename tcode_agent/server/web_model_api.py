"""Write-only credentials and explicit model capability checks for Web admins."""

import asyncio
import base64
import json
import os
import re
import struct
import zlib
from datetime import datetime, timezone
from random import SystemRandom
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from .. import web_model_settings as store
from ..config.manager import ConfigManager
from ..config.options import resolve_option_value
from ..providers.base import ChatMessage
from ..providers.web_model_transport import allow_local_models, guarded_client, local_web_binding, loopback_host, validate_base_url
from ..vision import model_config_supports_images
from . import workdirs
from .auth import auth_required, require_current_user
from .state import ServerState

router = APIRouter(prefix="/api/config/web-models", include_in_schema=False)
_test_lock = asyncio.Lock()


def _authorize(request: Request) -> None:
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        raise HTTPException(404, "此功能只用于 Web")
    host = request.url.hostname or ""
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"} or (
        origin and origin != f"{request.url.scheme}://{request.url.netloc}"
    ):
        raise HTTPException(403, "请从 TCode 设置页面发起请求")
    direct_local = (
        local_web_binding() and loopback_host(host) and request.client
        and request.client.host in {"127.0.0.1", "::1"}
        and not any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host"))
    )
    if not auth_required():
        if not direct_local:
            raise HTTPException(403, "请登录实例管理员，或通过本机回环地址访问")
        return
    actor = require_current_user(request)
    if actor.role not in {"owner", "admin"} or actor.tenant_id != workdirs.default_tenant_id():
        raise HTTPException(403, "需要实例管理员权限")
    if request.url.scheme != "https" and not direct_local:
        raise HTTPException(403, "共享部署请通过 HTTPS 管理模型")


async def _payload(request: Request) -> dict:
    _authorize(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "需要 JSON 请求")
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 32 * 1024:
                raise HTTPException(413, "模型配置过大")
            body.extend(chunk)
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, RecursionError):
        raise HTTPException(400, "模型配置格式无效") from None


def _config() -> dict:
    return ConfigManager.reload(ServerState.get_workdir(), emit=False)


def _view(settings: dict) -> dict:
    config = _config()
    providers = config.get("provider")
    providers = providers if isinstance(providers, dict) else {}
    rows = []
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
            continue
        for model, info in provider["models"].items():
            rows.append({
                "ref": f"{provider_id}/{model}",
                "label": f"{provider.get('name') or provider_id} / {model}",
                "supports_images": model_config_supports_images(info) is True,
            })
    services = []
    for sid, service in settings.get("services", {}).items():
        provider = providers.get(store.PREFIX + sid)
        options = _provider_options(provider)
        services.append({
            **{k: service[k] for k in ("name", "protocol", "base_url", "models")},
            "id": sid, "key_saved": bool(service.get("api_key")),
            "active": options.get("webModelService") == sid, "results": service.get("results", {}),
        })
    return {
        "services": services, "models": rows, "default_chat": config.get("model", ""),
        "default_vision": config.get("vision_model", ""),
        "overrides": {field: settings.get(field, "") for field in ("model", "vision_model")},
    }


def _read() -> dict:
    try:
        return store.read_settings()
    except (OSError, ValueError):
        raise HTTPException(409, "无法读取模型设置，原配置未覆盖") from None


def _change(callback) -> dict:
    try:
        return store.update_settings(callback)
    except (OSError, ValueError):
        raise HTTPException(409, "无法保存模型设置，原配置未覆盖") from None


def _find(config: dict, ref: str):
    provider_id, _, model = ref.partition("/")
    providers = config.get("provider")
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict) or model not in provider["models"]:
        raise HTTPException(404, "模型不存在，请刷新列表")
    return provider_id, model, provider


def _provider_options(provider) -> dict:
    options = provider.get("options") if isinstance(provider, dict) else None
    return options if isinstance(options, dict) else {}


def _string(value, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
        raise HTTPException(400, f"请检查{label}")
    return value.strip()


@router.get("")
def list_settings(request: Request):
    _authorize(request)
    return JSONResponse(_view(_read()), headers={"Cache-Control": "no-store"})


@router.post("/services")
async def save_service(request: Request):
    data = await _payload(request)
    sid = data.get("id")
    if sid is not None and (not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid)):
        raise HTTPException(400, "无效的服务标识")
    creating = sid is None
    sid = sid or uuid4().hex
    name = _string(data.get("name"), "服务名称", 100)
    protocol = data.get("protocol")
    if protocol not in ("openai_compatible", "ollama", "anthropic"):
        raise HTTPException(400, "请选择模型接口协议")
    try:
        base = validate_base_url(_string(data.get("base_url"), "接口地址", 1024), allow_loopback=allow_local_models())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if protocol == "ollama" and not base.endswith("/v1"):
        base += "/v1"
    values = data.get("models")
    if not isinstance(values, list) or not 1 <= len(values) <= 50:
        raise HTTPException(400, "请填写 1 至 50 个模型 ID")
    models = list(dict.fromkeys(_string(value, "模型 ID", 180) for value in values))
    key = data.get("api_key")
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[!-~]{0,4096}", key)):
        raise HTTPException(400, "API Key 格式无效")
    config = _config()
    prefix = store.PREFIX + sid + "/"
    def write(settings):
        services = settings.setdefault("services", {})
        prior = services.get(sid, {})
        if not creating and not prior:
            raise HTTPException(404, "模型服务不存在，请刷新列表")
        if creating and len(services) >= 32:
            raise HTTPException(400, "最多可添加 32 个模型服务")
        connection_changed = (prior.get("base_url"), prior.get("protocol")) != (base, protocol)
        if prior.get("api_key") and connection_changed and key is None:
            raise HTTPException(400, "更改地址或协议后，请重新填写 API Key，或明确清除原密钥")
        effective_key = prior.get("api_key", "") if key is None else key
        reset = connection_changed or effective_key != prior.get("api_key", "")
        for field in ("model", "vision_model"):
            selected = settings.get(field) or config.get(field) or ""
            if isinstance(selected, str) and selected.startswith(prefix) and selected[len(prefix):] not in models:
                raise HTTPException(409, "请先切换默认模型，再移除正在使用的模型 ID")
        results = {} if reset else {m: v for m, v in prior.get("results", {}).items() if m in models}
        services[sid] = {
            "name": name, "protocol": protocol, "base_url": base, "models": models, "api_key": effective_key,
            "results": results, "vision_verified": [] if reset else [m for m in prior.get("vision_verified", []) if m in models],
            "revision": uuid4().hex,
        }
        if reset and str(settings.get("vision_model", "")).startswith(prefix):
            settings.pop("vision_model")
    return _view(_change(write))


@router.put("/defaults")
async def set_defaults(request: Request):
    data = await _payload(request)
    field = data.get("purpose")
    if field not in ("model", "vision_model"):
        raise HTTPException(400, "请选择对话或图片用途")
    ref = data.get("ref")
    if not isinstance(ref, str) or len(ref) > 360:
        raise HTTPException(400, "请选择模型")
    config = _config()
    def write(settings):
        if not ref:
            settings.pop(field, None)
            return
        _, model, provider = _find(config, ref)
        sid = _provider_options(provider).get("webModelService")
        if sid:
            if not isinstance(sid, str):
                raise HTTPException(409, "模型配置无效")
            service = settings.get("services", {}).get(sid, {})
            if model not in service.get("models", []):
                raise HTTPException(409, "模型已变更，请刷新列表")
            if field == "vision_model" and model not in service.get("vision_verified", []):
                raise HTTPException(400, "请先通过图片测试")
        elif field == "vision_model" and model_config_supports_images(provider["models"][model]) is not True:
            raise HTTPException(400, "文件配置中的模型需先声明图片能力")
        settings[field] = ref
    return _view(_change(write))


def _image_challenge() -> tuple[str, str]:
    colors = [("red", (255, 0, 0)), ("blue", (0, 0, 255)), ("green", (0, 180, 0)), ("yellow", (255, 255, 0))]
    SystemRandom().shuffle(colors)
    def chunk(kind, body):
        return struct.pack("!I", len(body)) + kind + body + struct.pack("!I", zlib.crc32(kind + body))
    line = b"\x00" + b"".join(bytes(rgb) * 64 for _, rgb in colors)
    image = (b"\x89PNG\r\n\x1a\n"
             + chunk(b"IHDR", struct.pack("!IIBBBBB", 256, 128, 8, 2, 0, 0, 0))
             + chunk(b"IDAT", zlib.compress(line * 128)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii"), " ".join(name for name, _ in colors)


async def probe_model(*, base: str, key: str, protocol: str, model: str, purpose: str,
                      api_mode: str = "", trusted_config: bool = False) -> dict:
    """Exercise the real provider adapter, with no tools, history, or image fallback."""
    from ..providers.anthropic import AnthropicProvider
    from ..providers.openai import OpenAIProvider
    if trusted_config:
        # The endpoint comes from the server-owned deployment file, never the
        # browser. Preserve its DNS routing; still reject credentials in URLs
        # and never follow redirects from that exact configured endpoint.
        base = base.strip().rstrip("/")
        parts = urlsplit(base)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
                or re.search(r"[\s\\?#\x00-\x1f\x7f]", base)):
            raise ValueError("文件配置中的接口地址无效")
    else:
        base = validate_base_url(base, allow_loopback=allow_local_models())
    images = None
    expected = ""
    prompt = "Reply with only the word OK."
    if purpose == "image":
        image, expected = _image_challenge()
        images = [{"data_url": image, "source": "user_upload"}]
        prompt = "Read the four colored stripes in the image from left to right. Reply with only the four color names in English, separated by spaces. If no image is visible, reply NO IMAGE."
    reason = ""
    try:
        async with guarded_client(allow_loopback=allow_local_models(), probe=True, trusted_config=trusted_config) as client:
            opts = dict(api_key=key, base_url=base, model=model, timeout_seconds=40, http_client=client,
                        max_retries=0, use_env_key=False)
            provider = (AnthropicProvider(**opts) if protocol == "anthropic"
                        else OpenAIProvider(**opts, api_mode=api_mode or "chat_completions"))
            response = await asyncio.wait_for(
                provider.chat([ChatMessage(role="user", content=prompt, images=images)], max_tokens=256),
                timeout=42,
            )
        answer = str(response.content or "").strip()
        if not answer:
            reason = "模型没有返回可验证的文本，可能已用完本次测试的输出预算"
        elif purpose == "image" and " ".join(re.findall(r"[a-z]+", answer.lower())) != expected:
            reason = "模型未正确识别测试图的颜色顺序，不能确认图片能力"
    except Exception as error:
        # SDK adapters wrap failures; classify without echoing upstream headers, URLs or payloads.
        status = None
        timeout = False
        for _ in range(5):
            if error is None:
                break
            status = status or getattr(error, "status_code", None)
            timeout |= isinstance(error, (TimeoutError, httpx.TimeoutException)) or type(error).__name__ == "APITimeoutError"
            error = error.__cause__ or error.__context__
        reason = "请求超时，请检查网络或模型服务" if timeout else {
            400: "服务拒绝测试请求，请核对接口协议、模型 ID 和输入能力",
            401: "API Key 无效或已过期", 403: "服务拒绝访问，请检查模型权限",
            404: "接口路径或模型 ID 不存在", 429: "服务限流或额度不足",
        }.get(status, "未收到有效响应，请检查模型地址、网络和服务配置")
    return {"ok": not reason, "reason": reason, "tested_at": datetime.now(timezone.utc).isoformat()}


@router.post("/test")
async def test_model(request: Request):
    data = await _payload(request)
    ref, purpose = data.get("ref"), data.get("purpose")
    if not isinstance(ref, str) or len(ref) > 360 or purpose not in ("text", "image"):
        raise HTTPException(400, "请选择模型及测试类型")
    provider_id, model, provider = _find(_config(), ref)
    options = _provider_options(provider)
    sid = options.get("webModelService")
    if sid and (not isinstance(sid, str) or store.PREFIX + sid != provider_id):
        raise HTTPException(409, "模型配置无效")
    service = _read().get("services", {}).get(sid) if sid else None
    if sid and (not service or model not in service["models"]):
        raise HTTPException(409, "模型已变更，请刷新列表")
    if service:
        base, key, protocol = service["base_url"], service.get("api_key", ""), service["protocol"]
        mode = "chat_completions"
    else:
        npm = str(provider.get("npm", "")).lower()
        protocol = "anthropic" if "anthropic" in npm or provider_id.startswith("anthropic") else "openai_compatible"
        base = resolve_option_value(options, "baseURL", "base_url")
        key = resolve_option_value(options, "apiKey", "api_key")
        mode = resolve_option_value(options, "apiMode", "api_mode") or ("responses" if "openai-responses" in npm else "")
        if "ollama" in npm and not ("openai" in npm or options.get("baseURL")):
            base = (base or "http://localhost:11434").rstrip("/")
            base = base if base.endswith("/v1") else base + "/v1"
        if not base:
            raise HTTPException(400, "此文件模型未显式配置接口地址，不能在页面测试")
    if _test_lock.locked():
        raise HTTPException(429, "已有模型正在测试，请稍候")
    try:
        async with _test_lock:
            result = await probe_model(base=base, key=key, protocol=protocol, model=model, purpose=purpose,
                                       api_mode=mode, trusted_config=service is None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if service:
        def write(settings):
            current = settings.get("services", {}).get(sid)
            if not current or current["revision"] != service["revision"]:
                raise HTTPException(409, "测试期间模型配置已变更，请重新测试")
            current.setdefault("results", {}).setdefault(model, {})[purpose] = result
            if purpose == "image" and result["ok"] and model not in current.setdefault("vision_verified", []):
                current["vision_verified"].append(model)
        _change(write)
        _config()
    else:
        # File-backed results are deliberately session-only in the UI; do not
        # write into or shadow a deployment's credentials or capability flags.
        _, _, latest = _find(_config(), ref)
        latest_options = _provider_options(latest)
        if latest_options != options or resolve_option_value(latest_options, "apiKey", "api_key") != key:
            raise HTTPException(409, "测试期间文件配置已变更，请重新测试")
    return result
