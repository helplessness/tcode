"""Instance administrator settings; search credentials are write-only."""

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import aiohttp

from fastapi import APIRouter, HTTPException, Request

from ..atomic_storage import atomic_write_json, file_transaction
from .. import websearch_settings as config
from ..tools.builtin.websearch import WebSearchParameters, WebSearchTool
from .auth import auth_required, require_current_user, workdir_utils

router = APIRouter(prefix="/api/config/websearch")


def _authorize(request: Request) -> None:
    user = require_current_user(request)
    if auth_required() and (
        user.role not in {"owner", "admin"} or user.tenant_id != workdir_utils.default_tenant_id()
    ):
        raise HTTPException(403, "需要实例管理员权限")


def _public(settings: dict) -> dict:
    url = config.searxng_url(settings)
    # Never reflect credentials from legacy environment URLs.
    return {
        "provider": config.provider(settings),
        "searxng_url": url if _valid_url(url) else "",
        "brave_key_configured": bool(config.brave_key(settings)),
    }


def _valid_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return bool(
            len(value) <= 2048 and not re.search(r"[\s\\\x00-\x1f\x7f]", value)
            and url.scheme in {"http", "https"} and url.hostname
            and (url.port is None or url.port > 0) and url.username is None
            and "?" not in value and "#" not in value
        )
    except ValueError:
        return False


@router.get("")
def get_settings(request: Request):
    _authorize(request)
    try:
        return _public(config.read_settings())
    except (OSError, ValueError):
        raise HTTPException(409, "无法读取网页搜索配置") from None


@router.put("")
async def put_settings(request: Request):
    _authorize(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "网页搜索配置格式无效") from None
    if not isinstance(payload, dict) or payload.get("provider") not in ("searxng", "brave"):
        raise HTTPException(400, "请选择 SearXNG 或 Brave Search")
    selected = payload["provider"]
    field = "searxng_url" if selected == "searxng" else "brave_api_key"
    value = payload.get(field, "")
    if not isinstance(value, str):
        raise HTTPException(400, "网页搜索配置格式无效")
    value = value.strip()
    if value and selected == "searxng" and not _valid_url(value):
        raise HTTPException(400, "请输入不含凭据、查询参数或片段的 HTTP(S) 实例地址")
    if value and selected == "brave" and not re.fullmatch(r"[!-~]{1,512}", value):
        raise HTTPException(400, "Brave API Key 格式无效")
    path = config.settings_path()
    try:
        with file_transaction(path):
            settings = config.read_settings(path)
            if value:
                settings[field] = value
            if selected == "brave" and not config.brave_key(settings):
                raise HTTPException(400, "请填写 Brave API Key")
            settings["provider"] = selected
            atomic_write_json(path, settings)
    except (OSError, ValueError):
        raise HTTPException(409, "无法保存网页搜索配置") from None
    return _public(settings)


@router.post("/test")
async def test_connection(request: Request):
    _authorize(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "测试连接要求 JSON 请求")
    try:
        settings = config.read_settings()
    except (OSError, ValueError):
        raise HTTPException(409, "无法读取网页搜索配置") from None
    selected = config.provider(settings)
    tool = WebSearchTool()
    query = "Wikipedia"
    timeout = 8
    reason = ""
    if selected == "brave":
        key = config.brave_key(settings)
        if not key:
            reason = "请先保存 Brave API Key。"
        else:
            result = await tool._search_brave(WebSearchParameters(query=query), query, 1, timeout, key)
            reason = result.error or ""
            if not reason and not result.metadata.get("count"):
                reason = "Brave 未返回测试搜索结果，暂时无法确认搜索可用。"
    elif selected == "searxng":
        url = config.searxng_url(settings)
        if not _valid_url(url):
            reason = "请先保存有效的 SearXNG 实例地址。"
        else:
            try:
                async with aiohttp.ClientSession() as session:
                    await tool._fetch_page(
                        session=session, endpoint=urljoin(url.rstrip("/") + "/", "search"),
                        params={"q": query, "format": "json", "categories": "general", "language": "auto"},
                        headers={"Accept": "application/json", "User-Agent": "tcode-agent-websearch/1.0"},
                        timeout=timeout, page=1, probe=True,
                    )
            except RuntimeError as exc:
                reason = str(exc)
    else:
        reason = "请先保存支持的搜索服务。"
    try:
        if config.read_settings() != settings:
            raise HTTPException(409, "测试期间配置已变化，请重新打开设置后再测试。")
    except (OSError, ValueError):
        raise HTTPException(409, "无法确认当前网页搜索配置") from None
    return {"ok": not reason, "reason": reason, "tested_at": datetime.now(timezone.utc).isoformat()}
