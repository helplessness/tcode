"""Deployment-wide Artifact endpoints; tenant members cannot change routing."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
import httpx

from .auth import require_current_user
from .desktop_config import artifact_configuration
from . import workdirs

router = APIRouter(prefix='/api/artifact-services', include_in_schema=False)


def _config_path() -> Path:
    from ..config.manager import ConfigManager
    return Path(ConfigManager.resolve_config_root()) / 'artifact-services.json'


def load_artifact_services() -> None:
    """Apply saved Web configuration before any work is scheduled."""
    if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
        return
    source = _config_path()
    if not source.exists():
        return
    settings = artifact_configuration(json.loads(source.read_text(encoding='utf-8')), allow_loopback=True)
    os.environ.update(TMCP_ARTIFACT_SERVICES=json.dumps(settings['artifact_services']),
                      TMCP_ARTIFACT_SERVICE_ID=settings['artifact_default_service_id'],
                      TMCP_ARTIFACT_TARGET_SERVICES=json.dumps(settings['artifact_target_services']),
                      TMCP_ARTIFACT_SERVICE_URL=settings['artifact_services'].get(settings['artifact_default_service_id'], {}).get('url', ''))


def _can_edit(user) -> bool:
    return user.role in {'owner', 'admin'} and user.tenant_id == workdirs.default_tenant_id()


@router.post('/test')
async def test_service(request: Request):
    if not _can_edit(require_current_user(request)):
        raise HTTPException(403, '仅部署管理员可测试 Artifact 服务')
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, '需要 JSON 请求')
    from ..artifact_transport import _artifact_endpoint
    from .desktop_config import remote_services_paused

    def result(state, reason):
        return {'state': state, 'reason': reason, 'tested_at': datetime.now(timezone.utc).isoformat()}

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, '请求格式无效') from None
    service_id = payload.get('service_id') if isinstance(payload, dict) else None
    if not isinstance(service_id, str) or not service_id:
        raise HTTPException(400, '请选择已保存的文件服务')
    if remote_services_paused():
        return result('failed', '远端服务已暂停。')
    try:
        settings = artifact_configuration({}, allow_loopback=os.environ.get('TCODE_DESKTOP_MANAGED') != '1' or os.environ.get('TCODE_DESKTOP_DEV_LOCAL_SERVICES') == '1')
        entry = settings['artifact_services'].get(service_id)
        if not entry or any(payload.get(field) != entry[field] for field in ('url', 'token_env')):
            return result('unverified', '配置尚未生效，请保存并重启服务后再测试。')
        mapped = os.environ.get('TMCP_ARTIFACT_SERVICES', '').strip() not in {'', '{}'}
        _, url, headers = _artifact_endpoint(service_id if mapped else '')
        if url != entry['url']:
            return result('unverified', '配置尚未生效，请重启服务后再测试。')
    except (ValueError, RuntimeError, TypeError):
        return result('failed', '文件服务地址或凭据未配置，请检查服务配置和凭据环境变量。')
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(f'{url}/health', headers=headers)
        if response.status_code in (401, 403):
            return result('failed', '认证失败，请检查服务凭据与访问权限。')
        if response.status_code != 200:
            return result('failed', f'健康接口返回 HTTP {response.status_code}，请检查服务地址和状态。')
        health = response.json()
    except httpx.TimeoutException:
        return result('failed', '连接超时，请检查后端网络和服务地址。')
    except httpx.RequestError:
        return result('failed', '无法连接文件服务，请检查地址、网络与 TLS 配置。')
    except ValueError:
        return result('failed', '健康接口没有返回有效 JSON。')
    if not isinstance(health, dict) or health.get('status') != 'ok':
        return result('failed', '健康接口未返回预期的 Artifact 服务状态。')
    if health.get('mode') == 'restricted' and headers.get('X-Artifact-Token'):
        return result('connected', '服务可达，认证通过。未测试文件传输或 MCP 绑定。')
    return result('reachable', '健康检查通过，但未验证凭据、文件传输或 MCP 绑定。')


async def artifact_service_status() -> list[dict]:
    from .desktop_api import _probe_artifact_service
    settings = artifact_configuration({}, allow_loopback=os.environ.get('TCODE_DESKTOP_MANAGED') != '1' or os.environ.get('TCODE_DESKTOP_DEV_LOCAL_SERVICES') == '1')
    items = []
    limit = asyncio.Semaphore(16)
    async def probe(key, entry):
        item = {'service_id': key, 'name': entry['name'], 'url': entry['url'],
                'state': 'checking', 'default': key == settings['artifact_default_service_id']}
        items.append(item)
        async with limit:
            selected = key if os.environ.get('TMCP_ARTIFACT_SERVICES', '').strip() not in {'', '{}'} else ''
            item['state'] = await _probe_artifact_service(entry['url'], selected)
    try:
        await asyncio.wait_for(asyncio.gather(*(probe(key, entry) for key, entry in settings['artifact_services'].items())), timeout=22)
    except TimeoutError:
        pass
    return items


@router.get('')
async def status(request: Request):
    require_current_user(request)
    try:
        return {'artifacts': await artifact_service_status()}
    except (ValueError, TypeError):
        return {'artifacts': [], 'artifact': {'state': 'invalid', 'url': ''}}


@router.get('/config')
async def read_config(request: Request):
    user = require_current_user(request)
    source = _config_path()
    try:
        entry = json.loads(source.read_text(encoding='utf-8')) if source.exists() else {}
        return {**artifact_configuration(entry, allow_loopback=True), 'can_edit': _can_edit(user)}
    except (ValueError, TypeError, OSError) as exc:
        raise HTTPException(409, 'Artifact 配置无效，请检查服务器配置文件') from exc


@router.put('/config')
async def save_config(request: Request):
    if not _can_edit(require_current_user(request)):
        raise HTTPException(403, '仅默认部署租户的管理员可修改全局 Artifact 配置')
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('artifact_services'), dict):
            raise ValueError('Artifact 配置必须包含服务对象')
        settings = artifact_configuration({
            'artifact_services': payload['artifact_services'],
            'artifact_default_service_id': payload.get('artifact_default_service_id', ''),
            'artifact_target_services': payload.get('artifact_target_services', {}),
        }, allow_loopback=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    source = _config_path()
    source.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=source.parent, prefix='.artifact-services-', encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
        temporary.replace(source)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'saved': True, 'restart_required': True}
