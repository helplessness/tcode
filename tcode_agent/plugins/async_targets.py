"""Declarative asynchronous plugin targets, activated only by a host user."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from .registry import PluginRegistry


def declarations(root: Path, paths) -> list[dict]:
    targets = []
    identifiers = set()
    for name in paths:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('async target declaration escaped plugin root')
        if path.stat().st_size > 256_000:
            raise ValueError('async target declaration exceeds 256 KB')
        data = json.loads(path.read_text())
        schema_path = Path(__file__).resolve().parents[1] / 'plugin_sdk/schema/plugin-async-targets-v1.schema.json'
        Draft202012Validator(json.loads(schema_path.read_text())).validate(data)
        if data.get('version') != 1 or not isinstance(data.get('targets'), list):
            raise ValueError('async-targets requires version 1 and a targets array')
        for target in data['targets']:
            identifier = target.get('id', '')
            if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', identifier) or identifier in identifiers:
                raise ValueError('invalid or duplicate async target ID')
            identifiers.add(identifier)
            if not isinstance(target.get('connection'), dict) or not isinstance(target.get('tools'), list):
                raise ValueError('async target requires connection and tools')
            if target.get('dynamic_headers'):
                from ..mcp.dynamic_headers import validate_rules
                target['dynamic_headers'] = validate_rules(target['dynamic_headers'])
            targets.append(target)
    return targets


@contextmanager
def store(workdir):
    root = Path(workdir) / '.tcode'
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'plugin-async-targets.sqlite'
    db = sqlite3.connect(path, timeout=5)
    os.chmod(path, 0o600)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS bindings (owner TEXT, plugin TEXT, target TEXT, data TEXT, PRIMARY KEY(owner,plugin,target))')
        with db:
            yield db
    finally:
        db.close()


def binding(workdir, owner, plugin, target):
    with store(workdir) as db:
        row = db.execute('SELECT data FROM bindings WHERE owner=? AND plugin=? AND target=?', (owner, plugin, target)).fetchone()
    return json.loads(row[0]) if row else {}


def save_binding(workdir, owner, plugin, target, data):
    with store(workdir) as db:
        db.execute('INSERT OR REPLACE INTO bindings VALUES (?, ?, ?, ?)', (owner, plugin, target, json.dumps(data)))


def registration_id(workdir, owner, plugin, target):
    value = json.dumps([str(Path(workdir).resolve()), owner, plugin, target])
    return 'plugin-' + hashlib.sha256(value.encode()).hexdigest()[:32]


def gateway_url():
    explicit = os.environ.get('TCODE_PLUGIN_JOB_PROTOCOL_URL', '').strip()
    if explicit:
        return explicit.rstrip('/')
    from ..mcp.manager_auto import MCPManager
    config = MCPManager._server_configs.get('job-protocol-mcp', {})
    url = str(config.get('url') or 'http://127.0.0.1:10121/mcp')
    return url.removesuffix('/mcp').rstrip('/')


async def gateway(method, path, payload=None):
    from ..server.desktop_config import remote_services_paused
    if remote_services_paused():
        raise ValueError('桌面远端服务已暂停，未发送连接请求')
    token = registry_token()
    if not token:
        raise ValueError('请在 TCode 和本地 Job Protocol 配置相同的 TMCP_TARGET_REGISTRY_TOKEN 后重启')
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        response = await client.request(method, gateway_url() + path,
                                        headers={'X-Target-Registry-Token': token}, json=payload)
    if response.is_error:
        detail = response.json().get('detail', 'Job Protocol 注册请求失败')
        raise ValueError(str(detail))
    return response.json()


def registry_token():
    token = os.environ.get('TMCP_TARGET_REGISTRY_TOKEN', '').strip()
    if not token:
        root = Path(__file__).resolve().parents[2]
        legacy = root / '.tcode-local/target-registry.token'
        default = legacy if legacy.is_file() else root / '.tcode/credentials/target-registry.token'
        path = Path(os.environ.get('TCODE_TARGET_REGISTRY_TOKEN_FILE') or default)
        if path.is_file():
            token = path.read_text().strip()
    return token


def target_payload(workdir, owner, plugin, target, config, *, resolve_secrets=True):
    connection = target['connection']
    url = str(config.get(connection.get('urlConfig', 'compute_url'), '')).strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请填写不含密钥和查询参数的 HTTP(S) MCP 地址')
    token_env = str(config.get(connection.get('bearerTokenConfig', 'compute_token_env'), '')).strip()
    allowed = {name.strip() for name in os.environ.get('TCODE_PLUGIN_SECRET_ENVS', '').split(',') if name.strip()}
    if token_env and not (token_env.startswith('TCODE_PLUGIN_TOKEN_') or token_env in allowed):
        raise ValueError('仅允许 TCODE_PLUGIN_TOKEN_ 开头或管理员列入 TCODE_PLUGIN_SECRET_ENVS 的令牌变量，不能读取任意宿主密钥')
    token = os.environ.get(token_env, '') if token_env and resolve_secrets else ''
    if resolve_secrets and token_env and not token:
        raise ValueError('所选令牌环境变量尚未配置，请配置后重启 TCode')
    from ..mcp.dynamic_headers import validate_rules
    rules = validate_rules(config.get('dynamic_headers', target.get('dynamic_headers', {})), {'Authorization': ''} if token else {})
    from ..mcp.dynamic_headers import augment_schema
    for tool in target['tools']:
        augment_schema(tool['inputSchema'], rules)  # Refuse a host/remote parameter collision.
    return {'server': registration_id(workdir, owner, plugin, target['id']), 'owner': owner,
            'plugin': plugin, 'url': url, 'token': token, 'transport': connection.get('transport', 'streamable-http'),
            'execution': target['execution'], 'tools': target['tools'], **({'dynamic_headers': rules} if rules else {})}


def plugin_targets(loaded):
    manifest = loaded.manifest or {}
    # source is the package path, not a user-provided target path.
    root = Path(loaded.async_target_root or loaded.source).resolve()
    if root.is_file():
        root = root.parent
    return declarations(root, manifest.get('asyncTargets', []))


async def deactivate_plugin(workdir, plugin):
    from ..server.desktop_config import remote_services_paused
    with store(workdir) as db:
        rows = db.execute('SELECT owner,target,data FROM bindings WHERE plugin=?', (plugin,)).fetchall()
    for owner, target, raw in rows:
        data = json.loads(raw)
        if data.get('active'):
            if not remote_services_paused():
                await gateway('DELETE', '/api/v1/target-registrations/' + data['server'])
            data['active'] = False
            save_binding(workdir, owner, plugin, target, data)


def declaration_hash(target):
    return hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()


async def restore_plugin(workdir, loaded, *, inactive_only=False):
    """Restore only an explicitly approved, unchanged declaration on restart."""
    from ..server.desktop_config import remote_services_paused
    if remote_services_paused():
        return  # Preserve approval/bindings; no gateway registration or recovery.
    for target in plugin_targets(loaded):
        with store(workdir) as db:
            rows = db.execute('SELECT owner,data FROM bindings WHERE plugin=? AND target=?', (loaded.name, target['id'])).fetchall()
        for owner, raw in rows:
            state = json.loads(raw)
            if not state.get('approved') or state.get('declaration_hash') != declaration_hash(target):
                continue
            if inactive_only and state.get('active'):
                continue
            try:
                payload = target_payload(workdir, owner, loaded.name, target, state['config'])
                result = await gateway('PUT', '/api/v1/target-registrations/' + payload['server'], payload)
                if payload.get('dynamic_headers') and result.get('dynamic_headers') != payload['dynamic_headers']:
                    raise ValueError('请升级本地 Job Protocol 以支持动态 Header')
                state.update(active=True, version=result['version'], error='')
            except Exception:
                state.update(active=False, error='本地 Job Protocol 或远端 MCP 尚未就绪，请在设置中检查并重新启用')
            save_binding(workdir, owner, loaded.name, target['id'], state)


def find_target(workdir, server, owner=''):
    with store(workdir) as db:
        rows = db.execute('SELECT owner,plugin,target,data FROM bindings').fetchall()
    for binding_owner, plugin, target_id, raw in rows:
        data = json.loads(raw)
        if data.get('server') == server and (not owner or owner == binding_owner):
            if data.get('origin') == 'mcp-config':
                if not data.get('active'):
                    raise ValueError('自定义 MCP 文件传输已停用')
                return data['declaration']
            loaded = PluginRegistry.get(plugin)
            if not data.get('active') or loaded is None:
                raise ValueError('插件异步目标已停用')
            target = next((t for t in plugin_targets(loaded) if t['id'] == target_id), None)
            if target:
                return {**target, 'dynamic_headers': data.get('config', {}).get('dynamic_headers', target.get('dynamic_headers', {}))}
    return None


def approved_servers(workdir, owner):
    with store(workdir) as db:
        rows = db.execute('SELECT plugin,data FROM bindings WHERE owner=?', (owner,)).fetchall()
    return {data.get('alias', data['server']).lower() if data.get('origin') == 'mcp-config' else data['server']
            for plugin, raw in rows if (data := json.loads(raw)).get('active')
            and (data.get('origin') == 'mcp-config' or PluginRegistry.get(plugin))}


def approved_target_version(workdir, owner, server):
    with store(workdir) as db:
        rows = db.execute('SELECT plugin,data FROM bindings WHERE owner=?', (owner,)).fetchall()
    for plugin, raw in rows:
        state = json.loads(raw)
        if (state.get('active') and state.get('server') == server
                and (state.get('origin') == 'mcp-config' or PluginRegistry.get(plugin))):
            return str(state.get('version') or '')
    return ''
