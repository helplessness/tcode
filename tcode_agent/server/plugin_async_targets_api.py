"""User-approved plugin targets, scoped to the authenticated workspace."""
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request

from ..plugins import async_targets as targets
from ..plugins.manager import PluginManager
from ..plugins.registry import PluginRegistry
from ..runtime.workspace_registry import registry_workspace
from .auth import require_current_user
from .catalog_api import _resolve_agent_catalog_workdir
from .workspace_scope import _workspace_context_from_request

router = APIRouter(prefix='/api/plugins', tags=['plugin-async-targets'])


async def scope(request):
    require_current_user(request)
    context = _workspace_context_from_request(request)
    workdir = await _resolve_agent_catalog_workdir(request, workdir_name=request.query_params.get('workdir_name', ''))
    with registry_workspace(workdir):
        await PluginManager.get_global(workdir=workdir, ensure_configured=False)
    return workdir, f'{context.tenant_id}/{context.user_id}'


@router.get('/async-targets')
async def list_targets(request: Request):
    workdir, owner = await scope(request)
    rows = []
    with registry_workspace(workdir):
        for plugin in PluginRegistry.list_all():
            for target in targets.plugin_targets(plugin):
                state = targets.binding(workdir, owner, plugin.name, target['id'])
                connection = target['connection']
                config = state.get('draft_config', state.get('config', {}))
                rows.append({'plugin': plugin.name, 'target': target['id'], 'execution': target['execution'],
                             'tools': [t['name'] for t in target['tools']], 'active': state.get('active', False),
                             'server': state.get('server', targets.registration_id(workdir, owner, plugin.name, target['id'])),
                             'version': state.get('version', ''), 'error': state.get('error', ''),
                             'url': config.get(connection.get('urlConfig', 'compute_url'), ''),
                             'token_env': config.get(connection.get('bearerTokenConfig', 'compute_token_env'), ''),
                             'dynamic_headers': config.get('dynamic_headers', target.get('dynamic_headers', {})),
                             'has_draft': 'draft_config' in state,
                             'declaration_files': (plugin.manifest or {}).get('asyncTargets', [])})
    return {'targets': rows, 'gateway_url': targets.gateway_url(),
            'config_file': str(Path(workdir) / '.tcode/plugin-async-targets.sqlite'),
            'management_configured': bool(targets.registry_token())}


@router.post('/{plugin}/async-targets/{target_id}/{action}')
async def configure(plugin: str, target_id: str, action: str, request: Request):
    if action not in {'save', 'check', 'activate', 'deactivate'}:
        raise HTTPException(404, '未知操作')
    workdir, owner = await scope(request)
    with registry_workspace(workdir):
        loaded = PluginRegistry.get(plugin)
        target = next((t for t in targets.plugin_targets(loaded) if t['id'] == target_id), None) if loaded else None
    if target is None:
        raise HTTPException(404, '请先安装并加载声明此异步目标的插件')
    try:
        state = targets.binding(workdir, owner, plugin, target_id)
        identifier = targets.registration_id(workdir, owner, plugin, target_id)
        if action == 'deactivate':
            if state.get('active'):
                await targets.gateway('DELETE', '/api/v1/target-registrations/' + identifier)
            state.update(active=False, approved=False, error='')
            targets.save_binding(workdir, owner, plugin, target_id, state)
            return {'status': 'inactive'}
        body = await request.json()
        if action == 'activate' and body.get('approved') is not True:
            raise ValueError('请先确认允许向该远端服务发送计算输入')
        connection = target['connection']
        # Drafts never replace an approved registration or resolve a host secret.
        config = {connection.get('urlConfig', 'compute_url'): str(body.get('url', '')),
                  connection.get('bearerTokenConfig', 'compute_token_env'): str(body.get('token_env', '')),
                  'dynamic_headers': body.get('dynamic_headers', state.get('draft_config', state.get('config', {})).get('dynamic_headers', target.get('dynamic_headers', {})))}
        payload = targets.target_payload(workdir, owner, plugin, target, config, resolve_secrets=action != 'save')
        if action == 'save':
            config['dynamic_headers'] = payload.get('dynamic_headers', {})
            state['draft_config'] = config
            targets.save_binding(workdir, owner, plugin, target_id, state)
            return {'status': 'draft_saved', 'active': state.get('active', False)}
        path = '/api/v1/target-registrations/' + identifier
        if action == 'check':
            return await targets.gateway('POST', path + '/check', payload)
        result = await targets.gateway('PUT', path, payload)
        if payload.get('dynamic_headers') and result.get('dynamic_headers') != payload['dynamic_headers']:
            raise ValueError('本地 Job Protocol 未确认动态 Header 规则，请同步升级 TMCP 后重启')
        targets.save_binding(workdir, owner, plugin, target_id,
                             {'active': True, 'approved': True, 'server': identifier, 'version': result['version'],
                              'declaration_hash': targets.declaration_hash(target), 'config': config, 'error': ''})
        return {'status': 'active', 'server': identifier, 'version': result['version']}
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, '无法连接本地 Job Protocol，请检查启动状态和注册令牌') from exc
