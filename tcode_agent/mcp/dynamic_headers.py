"""Host-only business context. Never mutate a connection or process environment."""
from copy import deepcopy
import hashlib
import json
import re

CONTEXT = '_tcode_context'
_FORBIDDEN = {'authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'host',
              'content-length', 'content-type', 'accept', 'connection', 'transfer-encoding',
              'mcp-session-id', 'mcp-protocol-version', 'x-tmcp-service-token',
              'x-job-protocol-token', 'x-target-registry-token', 'x-tcode-job-worker-token', 'uid', 'x-openclaw-uid',
              'x-api-key', 'api-key', 'x-auth-token', 'x-user-id', 'x-tenant-id'}


def validate_rules(rules, headers=None):
    if not isinstance(rules, dict) or len(rules) > 32:
        raise ValueError('动态 Header 必须是对象，最多 32 项')
    names = {str(k).lower() for k in (headers or {})}
    result = {}
    for name, raw in rules.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[!#$%&\'*+.^_`|~0-9A-Za-z-]{1,128}', name)
                or name.lower() in _FORBIDDEN or name.lower() in names):
            raise ValueError('动态 Header 名称无效、重复或属于身份/协议保留字段')
        names.add(name.lower())
        if not isinstance(raw, dict) or set(raw) - {'variable', 'description', 'extraction_hint', 'required', 'max_length'}:
            raise ValueError('动态 Header 规则字段无效')
        variable = raw.get('variable', '')
        if not isinstance(variable, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', variable):
            raise ValueError('动态 Header 变量名必须是字母开头的标识符')
        description, hint = raw.get('description', ''), raw.get('extraction_hint', '')
        if not isinstance(description, str) or not description.strip() or len(description) > 1000 or not isinstance(hint, str) or len(hint) > 1000:
            raise ValueError('请填写动态变量业务含义，说明最多 1000 字符')
        required, limit = raw.get('required', True), raw.get('max_length', 128)
        if type(required) is not bool or type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError('动态变量必填状态或长度限制无效（1..4096）')
        normalized = dict(variable=variable, description=description.strip(), extraction_hint=hint.strip(), required=required, max_length=limit)
        previous = next((r for r in result.values() if r['variable'] == variable), None)
        if previous and previous != normalized:
            raise ValueError('相同变量必须使用相同的说明和约束')
        result[name] = normalized
    return result


def context_schema(rules):
    properties, required = {}, []
    for rule in validate_rules(rules).values():
        name = rule['variable']
        properties[name] = {'type': 'string', 'minLength': 1, 'maxLength': rule['max_length'],
                            'description': rule['description'] + '。' + rule['extraction_hint'] + '；从用户明确需求提取，缺失或有歧义时先询问，不猜测、不沿用上次值。'}
        if rule['required'] and name not in required:
            required.append(name)
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


def augment_schema(schema, rules):
    if not rules:
        return schema
    result = deepcopy(schema)
    if CONTEXT in result.get('properties', {}):
        raise ValueError('远端工具参数与宿主 _tcode_context 冲突')
    context = context_schema(rules)
    result.setdefault('properties', {})[CONTEXT] = context
    if context['required']:
        result['required'] = list(dict.fromkeys([*result.get('required', []), CONTEXT]))
    return result


def render(rules, values):
    rules = validate_rules(rules)
    if not isinstance(values, dict) or set(values) - {r['variable'] for r in rules.values()}:
        raise ValueError('调用上下文必须是对象且只能包含已配置变量')
    result = {}
    for header, rule in rules.items():
        value = values.get(rule['variable'])
        if value is None:
            if rule['required']:
                raise ValueError(f"缺少本次调用变量 {rule['variable']}，请先询问用户")
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > rule['max_length'] or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"调用变量 {rule['variable']} 为空、过长或含非法字符")
        result[header] = value
    return result


def split(arguments):
    result = deepcopy(arguments)
    values = result.pop(CONTEXT, None)
    for key in ('input_config', 'input', 'payload'):
        nested = result.get(key)
        if isinstance(nested, dict) and CONTEXT in nested:
            if values is not None:
                raise ValueError('调用上下文不能在多处重复声明')
            values = nested.pop(CONTEXT)
    return result, values if values is not None else {}


def rules_for(server, context=None):
    from .manager_auto import MCPManager
    entry = next((entry for name, entry in MCPManager._server_configs.items() if name.lower() == server.lower()), {})
    if entry:
        return validate_rules(entry.get('dynamic_headers', {}), entry.get('headers'))
    if server.startswith('plugin-') and context is not None:
        from .custom_targets import pinned_target
        target = pinned_target(context, server)
        if not target:
            from ..plugins.async_targets import find_target
            from ._utils import _context_control_workdir, _context_workspace_claims
            claims = _context_workspace_claims(context)
            target = find_target(_context_control_workdir(context), server, f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}")
        return validate_rules((target or {}).get('dynamic_headers', {}))
    return {}


def snapshot(server, arguments, context=None):
    rules = rules_for(server, context)
    if not rules:
        return deepcopy(arguments), {}  # No opt-in: preserve the existing remote argument contract.
    business, values = split(arguments)
    render(rules, values)
    call_context = {'rules': rules, 'values': values} if rules else {}
    if rules and server.startswith('plugin-') and context is not None:
        from ..plugins import async_targets as targets
        from ._utils import _context_control_workdir, _context_workspace_claims
        claims = _context_workspace_claims(context)
        owner = f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}"
        with targets.store(_context_control_workdir(context)) as db:
            rows = db.execute('SELECT data FROM bindings WHERE owner=?', (owner,)).fetchall()
        for (raw,) in rows:
            state = json.loads(raw)
            if state.get('server') == server and state.get('version'):
                call_context['target_version'] = state['version']
                break
    return business, call_context


def fingerprint_arguments(server, arguments, context=None, *, values_from=None):
    _, frozen = snapshot(server, values_from if values_from is not None else arguments, context)
    if not frozen:
        return arguments
    digest = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'arguments': arguments, 'call_context': 'sha256://' + digest}


async def ensure_broker_support(server, tool, context):
    """Fail closed rather than let an older broker silently drop call_context."""
    from .manager_auto import MCPManager
    from ._utils import _context_workspace_claims
    claims = _context_workspace_claims(context)
    result = await MCPManager.call_tool('job-protocol-mcp', 'describe_target_tool', {'input': {
        'target_server': server, 'target_tool': tool, 'detail': 'compact', 'fields': [],
        **claims, 'issuer': 'tcode', 'subject': claims.get('user_id', '')}})
    payload = result.get('structuredContent')
    if not isinstance(payload, dict):
        try:
            payload = json.loads(next(b['text'] for b in result.get('content', []) if b.get('type') == 'text'))
        except (StopIteration, ValueError, TypeError):
            payload = {}
    if result.get('isError') or payload.get('call_context_supported') is not True:
        raise ValueError('本地 Job Protocol 尚不支持动态 Header，请同步更新 TMCP 并重启 Job Protocol；未提交任务')
