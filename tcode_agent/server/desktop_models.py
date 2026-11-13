"""Desktop model settings; credentials arrive only over the native gated route."""
import asyncio
import json
import math
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from fastapi import HTTPException

from ..config.manager import ConfigManager
from ..config.options import resolve_option_value
from ..providers.opencode_go import opencode_go_headers
from .config_files import _load_tcode_config
from .state import ServerState

LOCK = asyncio.Lock()


def validate_service(payload):
    sid = str(payload.get('id') or '')
    if not re.fullmatch(r'[a-z0-9-]{1,64}', sid):
        raise HTTPException(400, '服务标识无效')
    kind, protocol = payload.get('kind'), payload.get('protocol')
    if kind not in {'chat', 'embedding'} or protocol not in {'openai_compatible', 'ollama', 'anthropic'}:
        raise HTTPException(400, '请选择有效的模型用途和接口协议')
    if kind == 'embedding' and protocol == 'anthropic':
        raise HTTPException(400, '此协议不支持嵌入模型')
    base = str(payload.get('base_url') or '').strip().rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise HTTPException(400, '请填写不含凭据、查询参数的 HTTP(S) 接口地址')
    if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise HTTPException(400, '远程模型服务必须使用 HTTPS；本地服务可使用 HTTP')
    models = payload.get('models')
    if not isinstance(models, list) or not 1 <= len(models) <= 50:
        raise HTTPException(400, '请至少填写一个模型 ID，最多 50 个')
    models = list(dict.fromkeys(str(m).strip() for m in models))
    if any(not m or len(m) > 180 or any(ord(c) < 32 for c in m) for m in models):
        raise HTTPException(400, '模型 ID 不能为空或包含控制字符')
    name = str(payload.get('name') or '').strip()
    if not name or len(name) > 100:
        raise HTTPException(400, '请填写服务名称（不超过 100 字）')
    return dict(id=sid, name=name, kind=kind, protocol=protocol, base_url=base, models=models)


def secret_name(sid):
    return 'TCODE_DESKTOP_MODEL_' + sid.replace('-', '_').upper() + '_KEY'


def provider(service):
    base = service['base_url']
    protocol = service['protocol']
    if protocol == 'ollama' and service['kind'] == 'chat' and not base.endswith('/v1'):
        base += '/v1'
    return {'npm': '@ai-sdk/anthropic' if protocol == 'anthropic' else '@ai-sdk/openai-compatible',
            'options': {'baseURL': base, 'apiKeyEnv': secret_name(service['id'])},
            'models': {m: {'name': m} for m in service['models']}}


def settings_view(config):
    services = config.get('desktop_model_services', {})
    rows = [{**s, 'key_saved': bool(os.environ.get(secret_name(s['id'])))} for s in services.values()]
    from ..memory.embedding_provider import EmbeddingProvider
    return {'services': rows, 'embedding_local_only': EmbeddingProvider.local_embedding_enabled(), 'default_chat': config.get('model', ''),
            'default_embedding': config.get('desktop_embedding_default', ''),
            'embedding_enabled': bool(config.get('embeddings', {}).get('enabled'))}


async def probe(service, key, model):
    if model not in service['models']:
        raise HTTPException(400, '请选择此服务中的模型')
    base = service['base_url']
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    if service['kind'] == 'embedding':
        if service['protocol'] == 'ollama':
            url, body = base.removesuffix('/v1') + '/api/embed', {'model': model, 'input': ['TCode connection test']}
        else:
            url, body = base + '/embeddings', {'model': model, 'input': ['TCode connection test'], 'encoding_format': 'float'}
    elif service['protocol'] == 'anthropic':
        url = base.removesuffix('/v1') + '/v1/messages'
        headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'}
        body = {'model': model, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': 'Reply OK'}]}
    else:
        if service['protocol'] == 'ollama' and not base.endswith('/v1'):
            base += '/v1'
        url, body = base + '/chat/completions', {'model': model, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': 'Reply OK'}]}
        headers.update(opencode_go_headers(base))
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as client:
            async with client.post(url, json=body, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    messages = {401: '密钥无效或已过期', 403: '服务拒绝访问，请检查账号权限',
                                404: '接口路径或模型 ID 不存在', 429: '服务限流或额度不足'}
                    raise HTTPException(400, messages.get(response.status, f'服务返回 HTTP {response.status}，请检查接口配置'))
                raw = await response.content.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError()
                data = json.loads(raw)
                if service['kind'] == 'embedding':
                    vector = (data.get('embeddings') or [None])[0] if service['protocol'] == 'ollama' else (data.get('data') or [{}])[0].get('embedding')
                    if (not isinstance(vector, list) or not vector or len(vector) > 8192
                            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)
                            or not any(vector)):
                        raise ValueError()
                    return {'ok': True, 'message': f'嵌入模型连接成功，向量维度 {len(vector)}'}
                if not data.get('choices') and not data.get('content'):
                    raise ValueError()
                return {'ok': True, 'message': '聊天模型连接成功'}
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(400, '连接超时，请检查服务地址或网络') from None
    except aiohttp.ClientError:
        raise HTTPException(400, '无法连接模型服务，请检查地址、网络和证书') from None
    except (ValueError, TypeError, AttributeError, IndexError):
        raise HTTPException(400, '服务返回的格式与所选协议不兼容') from None


async def model_settings(payload):
    async with LOCK:
        workdir = ServerState.get_workdir()
        config = _load_tcode_config(workdir)
        action = payload.get('action')
        if action == 'list':
            return settings_view(config)
        service = validate_service(payload)
        key = payload.get('api_key')
        if key is not None and (not isinstance(key, str) or len(key) > 8192 or '\n' in key or '\r' in key):
            raise HTTPException(400, '密钥格式无效')
        if action == 'test':
            # Never use another saved service's key for an edited endpoint.
            if key is None:
                old = config.get('desktop_model_services', {}).get(service['id'])
                if not old or old['base_url'] != service['base_url'] or old['protocol'] != service['protocol']:
                    raise HTTPException(400, '地址或协议已改变，请重新填写密钥再测试')
                key = os.environ.get(secret_name(service['id']), '')
            return await probe(service, key, payload.get('model_id') or service['models'][0])
        if action not in {'save', 'default'}:
            raise HTTPException(400, '未知操作')
        # Only write the desktop config root, never merge and persist workspace
        # overrides or credentials from developer/production configuration.
        path = Path(ConfigManager.resolve_config_root(workdir)) / 'tcode.json'
        stored = json.loads(path.read_text()) if path.exists() else {}
        services = stored.setdefault('desktop_model_services', {})
        prior = services.get(service['id'])
        if action == 'default':
            if not prior or prior != service:
                raise HTTPException(400, '请先保存此服务，再设置默认模型')
        elif prior and (prior['base_url'] != service['base_url'] or prior['protocol'] != service['protocol']) and key is None:
            raise HTTPException(400, '地址或协议已改变，请重新填写密钥（本地无密钥服务可留空并选择清除密钥）')
        services[service['id']] = service
        if service['kind'] == 'chat':
            current = str(stored.get('model', ''))
            prefix = 'desktop-' + service['id'] + '/'
            if current.startswith(prefix) and current[len(prefix):] not in service['models']:
                raise HTTPException(400, '该模型当前为默认模型，请先切换默认模型再移除')
            stored.setdefault('provider', {})['desktop-' + service['id']] = provider(service)
        model = payload.get('model_id') or service['models'][0]
        if model not in service['models']:
            raise HTTPException(400, '默认模型不在此服务中')
        if action == 'default':
            if service['kind'] == 'chat':
                stored['model'] = 'desktop-' + service['id'] + '/' + model
            else:
                stored['desktop_embedding_default'] = service['id'] + '/' + model
                stored['embeddings'] = {'enabled': True, 'provider': 'ollama' if service['protocol'] == 'ollama' else 'openai_compatible',
                                        'model': model, 'options': {'baseURL': service['base_url'], 'apiKeyEnv': secret_name(service['id'])}}
        elif service['kind'] == 'embedding' and prior != service and str(stored.get('desktop_embedding_default', '')).startswith(service['id'] + '/'):
            raise HTTPException(400, '请添加新服务并切换默认模型；当前默认嵌入服务保持不变，以免静默改变索引来源')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.models-')
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(stored, file, ensure_ascii=False, indent=2)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        if key is not None:
            os.environ[secret_name(service['id'])] = key
        ConfigManager.reload(workdir)
        return {**settings_view(_load_tcode_config(workdir)), 'message': '已保存。更换嵌入模型后请重建相关向量索引；不会自动删除或重建已有数据。' if service['kind'] == 'embedding' and action == 'default' else '已保存，后续新任务使用新配置；进行中的任务不受影响。'}
