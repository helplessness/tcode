"""Deployment-wide Artifact endpoints; tenant members cannot change routing."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

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
