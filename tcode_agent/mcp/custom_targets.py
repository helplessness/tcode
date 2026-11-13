"""Optional custom-MCP input transport, using the existing target registry.

Registrations are immutable per configuration. Retaining a previous registration
lets an already queued LOCAL issue reach the broker with its original contract.
Only the current owner binding is exposed for NEW host submissions.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat

from jsonschema import Draft202012Validator

from ..plugins import async_targets as targets

ORIGIN = "mcp-config"


def fingerprint(entry, target):
    from .manager_auto import MCPManager
    normalized = {**entry, "headers": MCPManager._resolve_header_environment(entry.get("headers") or {})}
    return targets.declaration_hash({"entry": normalized, "target": target})


def fields_for_schema(schema: dict, prefix="", label="", depth=0) -> list[dict]:
    """Selectable properties, including nested objects and lists of files.

    Arrays of objects are deliberately not guessed: this first version supports
    named nested fields and whole file lists, not wildcard JSON pointers.
    """
    if depth > 16:
        raise ValueError("工具参数嵌套超过支持的深度")
    if not isinstance(schema, dict):
        return []
    variants = schema.get("anyOf") or schema.get("oneOf") or []
    objects = [s for s in [schema, *variants] if isinstance(s, dict) and isinstance(s.get("properties"), dict)]
    result = []
    seen = set()
    for node in objects:
        for name, child in node["properties"].items():
            pointer = prefix + "/" + str(name).replace("~", "~0").replace("/", "~1")
            title = f"{label} → {name}" if label else str(name)
            nested = fields_for_schema(child, pointer, title, depth + 1)
            # An object may itself be an ArtifactRef. Offer that property too.
            if pointer not in seen:
                result.append({"pointer": pointer, "label": title, "schema": child})
                seen.add(pointer)
            for item in nested:
                if item["pointer"] not in seen:
                    result.append(item)
                    seen.add(item["pointer"])
    return result


async def discover(entry: dict) -> list[dict]:
    """Handshake and schema discovery only; NEVER call computation tools."""
    from ..server.desktop_config import remote_services_paused
    if remote_services_paused():
        raise ValueError("桌面远端服务已暂停，未发送连接请求")
    if entry.get("transport", "streamable-http") != "streamable-http":
        raise ValueError("自定义文件传输第一版仅支持 Streamable HTTP；原有 SSE 接入不受影响")
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from .manager_auto import MCPManager
    headers = MCPManager._resolve_header_environment(entry.get("headers") or {})
    async with Client(StreamableHttpTransport(entry["url"], headers=headers), timeout=15) as client:
        tools = await client.list_tools()
    if len(tools) > 100:
        raise ValueError("自定义文件传输最多支持 100 个工具")
    result = []
    for tool in tools:
        schema = tool.inputSchema
        from .dynamic_headers import augment_schema
        augment_schema(schema, entry.get('dynamic_headers', {}))
        # Use the registry's inline-schema restriction, without resolving refs.
        def check(node):
            if isinstance(node, dict):
                if any(k in node for k in ("$ref", "$dynamicRef", "$id")):
                    raise ValueError("工具 Schema 含引用，请服务发布内联 Schema 后再配置文件传输")
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(schema)
        Draft202012Validator.check_schema(schema)
        result.append({"name": tool.name, "inputSchema": schema, "fields": fields_for_schema(schema)})
    if len(json.dumps(result).encode()) > 512_000:
        raise ValueError("工具 Schema 超过文件配置的大小限制")
    return result


def declaration(settings: dict, discovered: list[dict]) -> dict:
    if not isinstance(settings, dict) or settings.get("receiver") not in {"artifact_refs", "server_paths"}:
        raise ValueError("请选择远端文件接收方式")
    selected = settings.get("tools")
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
        raise ValueError("请至少选择一个需要异步执行的工具")
    available = {tool["name"]: tool for tool in discovered}
    tools, seen = [], set()
    for selection in selected:
        if not isinstance(selection, dict) or selection.get("name") not in available or selection["name"] in seen:
            raise ValueError("工具不存在或重复，请重新发现工具")
        seen.add(selection["name"])
        actual = available[selection["name"]]
        candidates = {f["pointer"]: f["schema"] for f in actual["fields"]}
        files = selection.get("files")
        if not isinstance(files, list) or len(files) > 256:
            raise ValueError("文件字段必须是列表且不超过 256 项")
        pointers = set()
        for field in files:
            if not isinstance(field, dict) or set(field) != {"pointer", "kind", "transport"}:
                raise ValueError("文件声明只能包含所选参数、类型和传输方式")
            pointer, kind = field["pointer"], field["kind"]
            if pointer not in candidates or pointer in pointers or kind not in {"file", "directory"} or field["transport"] != "artifact":
                raise ValueError("文件字段不在工具 Schema 中、重复或类型无效")
            if any(pointer.startswith(p + "/") or p.startswith(pointer + "/") for p in pointers):
                raise ValueError("不能同时选择父参数和其子参数")
            pointers.add(pointer)
            if settings["receiver"] == "artifact_refs":
                sample = placeholder_ref(kind)
                if not accepts_reference(candidates[pointer], sample):
                    raise ValueError("所选参数的 Schema 不接受产物引用，文件传输尚未接通；请先配置服务器适配器或修正服务契约")
        tools.append({"name": selection["name"], "argumentStyle": "flat", "inputSchema": actual["inputSchema"],
                      "files": deepcopy(files), "output": {"contract": "json"}})
    return {"id": "custom", "origin": ORIGIN, "execution": {"mode": "blocking"}, "tools": tools}


async def configure(workdir, owner, name, entry, old):
    settings = entry.get("file_transfer")
    if settings is None:
        if old.get("file_transfer"):
            state = targets.binding(workdir, owner, ORIGIN, name)
            state["active"] = False
            targets.save_binding(workdir, owner, ORIGIN, name, state)
        return
    if not isinstance(settings, dict) or settings.get('receiver') not in {'artifact_refs', 'server_paths'}:
        raise ValueError("文件传输配置无效；关闭传输请明确设置为 null")
    if entry.get("invocation_mode") != "async":
        raise ValueError("输入文件传输只能用于异步执行；切回直接调用前请关闭此选项")
    if not entry.get('enabled', True):
        state = targets.binding(workdir, owner, ORIGIN, name)
        state['active'] = False
        targets.save_binding(workdir, owner, ORIGIN, name, state)
        return  # Stopping NEW submissions must work even when the remote is offline.
    target = declaration(settings, await discover(entry))
    if entry.get('dynamic_headers'):
        target['dynamic_headers'] = deepcopy(entry['dynamic_headers'])
    active = entry.get("enabled", True) and settings["receiver"] == "artifact_refs"
    # Hash includes connection/credentials, so changes cannot redirect queued work.
    digest = fingerprint(entry, target)
    identifier = targets.registration_id(workdir, owner, ORIGIN, name + ":" + digest)
    if active:
        from .manager_auto import MCPManager
        payload = {"server": identifier, "owner": owner, "plugin": ORIGIN, "origin": ORIGIN,
                   "url": entry["url"], "headers": MCPManager._resolve_header_environment(entry.get("headers") or {}),
                   "transport": entry["transport"], "execution": target["execution"], "tools": target["tools"]}
        if target.get('dynamic_headers'):
            payload['dynamic_headers'] = target['dynamic_headers']
        result = await targets.gateway("PUT", "/api/v1/target-registrations/" + identifier, payload)
        if not isinstance(result, dict) or not result.get("version"):
            raise ValueError("Job Protocol 未确认注册版本；未启用文件传输")
        if payload.get('dynamic_headers') and result.get('dynamic_headers') != payload['dynamic_headers']:
            raise ValueError('本地 Job Protocol 未确认动态 Header 规则，请同步升级 TMCP 后重启')
    targets.save_binding(workdir, owner, ORIGIN, name,
                         {"active": bool(active), "origin": ORIGIN, "alias": name, "server": identifier,
                          "fingerprint": digest, "declaration": target,
                          **({'version': result['version']} if active else {})})


def configured_alias(name, context):
    """Owner-scoped name mapping only; this never grants route permission."""
    from ._utils import _context_control_workdir, _context_workspace_claims
    if name.startswith('plugin-'):
        claims = _context_workspace_claims(context)
        owner = f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}"
        with targets.store(_context_control_workdir(context)) as db:
            rows = db.execute('SELECT target,data FROM bindings WHERE owner=? AND plugin=?', (owner, ORIGIN)).fetchall()
        for alias, raw in rows:
            if json.loads(raw).get('server') == name:
                return alias
    return name


def resolve(name, context):
    """Resolve and pin one user-approved alias, before any file is read."""
    from .manager_auto import MCPManager
    from ._utils import _context_control_workdir, _context_workspace_claims
    alias = configured_alias(name, context)
    if alias != name:
        resolved = resolve(alias, context)
        if resolved != name:
            raise ValueError("原自定义 MCP 配置已失效，请使用当前服务配置；未上传或提交")
        return resolved
    name = next((key for key in MCPManager._server_configs if key.lower() == name.lower()), name)
    entry = MCPManager._server_configs.get(name, {})
    if not entry.get("file_transfer"):
        return name
    if not entry.get("enabled", True) or entry.get("invocation_mode") != "async":
        raise ValueError("自定义 MCP 已停用或不再采用异步执行，未上传或提交")
    if entry["file_transfer"].get("receiver") != "artifact_refs":
        raise ValueError("文件传输尚未接通：此服务只接受服务器路径，需要先部署适配器；未上传或提交")
    claims = _context_workspace_claims(context)
    state = targets.binding(_context_control_workdir(context), f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}", ORIGIN, name)
    digest = fingerprint(entry, state.get("declaration"))
    if not state.get("active") or digest != state.get("fingerprint"):
        raise ValueError("文件传输尚未在当前用户和工作区启用，或配置已变化；请重新保存并连接，未上传或提交")
    pinned = dict(getattr(context, "_custom_mcp_targets", {}))
    pinned[state["server"]] = deepcopy(state["declaration"])
    context._custom_mcp_targets = pinned
    return state["server"]


def pinned_target(context, server):
    return deepcopy(getattr(context, "_custom_mcp_targets", {}).get(server))


def placeholder_ref(kind):
    return ({'kind': 'artifact', 'artifact_id': 'art_' + '0' * 32} if kind == 'file'
            else {'kind': 'artifact_bundle', 'bundle_id': 'bundle_' + '0' * 32})


def accepts_reference(schema, sample):
    # Optional lists commonly appear as anyOf: [array, null]. Check their
    # element contract, retaining cardinality/union checks for actual arguments.
    if schema.get('type') == 'array':
        return Draft202012Validator(schema.get('items', {})).is_valid(sample)
    if Draft202012Validator(schema).is_valid(sample):
        return True
    return any(accepts_reference(s, sample) for s in schema.get('anyOf', schema.get('oneOf', [])))


def validation_arguments(context, server, tool, arguments):
    from ..workspace.input_resources import declared_resource_paths
    from ..workspace.async_protein_run import resolve_context_input_path
    result = deepcopy(arguments)
    for parts, kind in declared_resource_paths(server, tool, arguments, context=context).items():
        parent = result
        for part in parts[:-1]:
            parent = parent[part]
        path = resolve_context_input_path(context, parent[parts[-1]])
        if path is None or not (path.is_file() if kind == 'file' else path.is_dir()):
            raise ValueError('声明的输入文件或目录不存在，未上传或提交')
        parent[parts[-1]] = placeholder_ref(kind)
    return result


def host_input_schema(server, tool, schema):
    """Local UI/agent may supply paths; remote schema remains unchanged.

    Final broker validation still uses the discovered schema AFTER conversion.
    No remote JSON contract is weakened by this host-only parameter view.
    """
    from .manager_auto import MCPManager
    settings = MCPManager._server_configs.get(server, {}).get('file_transfer') or {}
    entry = next((t for t in settings.get('tools', []) if t.get('name') == tool), None)
    if not entry:
        return schema
    result = deepcopy(schema)
    for field in entry['files']:
        nodes = [result]
        parts = field['pointer'][1:].split('/')
        for part in parts:
            key = part.replace('~1', '/').replace('~0', '~')
            children = []
            for node in nodes:
                variants = [node, *node.get('anyOf', []), *node.get('oneOf', [])]
                children.extend(n['properties'][key] for n in variants if key in n.get('properties', {}))
            nodes = children
        def adapt(node):
            original = deepcopy(node)
            if node.get('type') == 'array':
                node['items'] = {'anyOf': [{'type': 'string'}, original.get('items', {})]}
            elif (variants := node.get('anyOf', node.get('oneOf', []))) and all(
                variant.get('type') in {'array', 'null'} for variant in variants
            ) and any(variant.get('type') == 'array' for variant in variants):
                for variant in variants:
                    if variant.get('type') == 'array':
                        adapt(variant)
            else:
                node.clear()
                node.update({'anyOf': [{'type': 'string'}, original],
                             'description': '本地输入路径或已有产物引用；读取与上传仍需授权'})
        for node in nodes:
            adapt(node)
    return result


async def authorize_inputs(context, server, tool, arguments):
    """Declarations are not read grants. Request permission before opening inputs."""
    target = pinned_target(context, server)
    if not target:
        return True
    context._custom_mcp_read_bindings = []
    context._custom_mcp_read_grants = []
    from ..workspace.input_resources import declared_resource_paths
    from ..workspace.async_protein_run import resolve_context_input_path
    paths = declared_resource_paths(server, tool, arguments, context=context)
    patterns = []
    bindings = []
    for parts, kind in paths.items():
        value = arguments
        for part in parts:
            value = value[part]
        path = resolve_context_input_path(context, value)
        if path is None:
            raise ValueError("声明的输入不存在或不可访问，未读取或上传")
        patterns.append(str(path / "**") if kind == "directory" else str(path))
        bindings.append({'argument_path': list(parts), 'path': str(path), 'kind': kind,
                         'identity': _read_input_identity(path, kind)})
    patterns = sorted(set(patterns))
    allowed = not patterns or await context.ask("read", patterns)
    if allowed:
        validate_read_bindings(context, server, tool, arguments, bindings=bindings)
        context._custom_mcp_read_grants = patterns
        context._custom_mcp_read_bindings = bindings
    return allowed


async def restore_review_inputs(context, server, tool, arguments, grants, bindings=None):
    """A computation approval cannot silently authorize a different input file."""
    from ._utils import _context_control_workdir, _context_workspace_claims
    claims = _context_workspace_claims(context)
    target = targets.find_target(_context_control_workdir(context), server,
                                 owner=f"{claims.get('tenant_id', '')}/{claims.get('user_id', '')}")
    if not target or target.get('origin') != ORIGIN or not isinstance(grants, list) or not all(isinstance(v, str) for v in grants):
        raise ValueError("原文件读取授权或自定义工具配置不可用，请重新申请；未读取或上传")
    context._custom_mcp_targets = {server: target}
    # Do not manufacture a new identity for a file authorized before restart.
    # Already queued ArtifactRefs require no local binding and remain unchanged.
    context._custom_mcp_read_bindings = []
    context._custom_mcp_read_grants = []
    bindings = deepcopy(bindings or [])
    used = validate_read_bindings(context, server, tool, arguments, bindings=bindings)
    required = {str(Path(b['path']) / '**') if b['kind'] == 'directory' else b['path'] for b in used}
    if not required <= set(grants):
        raise ValueError("批准后的输入超出原读取授权，请重新申请；未读取或上传")
    context.extra['__permission_ask__'] = lambda permission, patterns: permission == 'read' and set(patterns) <= set(grants)
    context._custom_mcp_read_bindings = bindings
    context._custom_mcp_read_grants = sorted(required)


def file_identity(info):
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


_WINDOWS = os.name == 'nt'


def _windows_input_io():
    try:
        from tcode_artifacts.windows_io import input_identity, open_file
    except ImportError as exc:
        raise ValueError('Windows 上传本地输入需要安装当前仓库的 Artifact SDK；未读取或上传') from exc
    return input_identity, open_file


def _read_input_identity(path, kind):
    if not _WINDOWS:
        return file_identity(path.stat())
    input_identity, _ = _windows_input_io()
    try:
        return input_identity(path, directory=kind == 'directory')
    except (OSError, ValueError) as exc:
        raise ValueError('无法安全核对 Windows 输入文件；请检查本地 NTFS 路径和访问权限，未读取或上传') from exc


def exact_fingerprint_arguments(arguments):
    """Keep custom JSON opaque to legacy path-normalizing dedupe helpers."""
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    # The existing helper exempts URI tokens from filesystem canonicalization.
    return {'custom_mcp_arguments_sha256': 'sha256://' + hashlib.sha256(encoded.encode()).hexdigest()}


def submission_guard_arguments(context, arguments):
    from .artifact_boundary import _directory_files
    arguments = deepcopy(arguments)
    bindings = deepcopy(getattr(context, '_custom_mcp_read_bindings', []))
    for binding in bindings:
        # Only declared, authorized local paths have path semantics. All other
        # strings (including whitespace and ../) remain exact JSON values.
        parent = arguments
        for part in binding['argument_path'][:-1]:
            parent = parent[part]
        parent[binding['argument_path'][-1]] = binding['path']
        if binding['kind'] == 'directory':
            root = Path(binding['path'])
            binding['entries'] = [(p.relative_to(root).as_posix(), _read_input_identity(p, 'file'))
                                  for p in sorted(_directory_files(root))]
    return exact_fingerprint_arguments({'custom_preupload_arguments': arguments, 'authorized_inputs': bindings})


def validate_read_bindings(context, server, tool, arguments, *, bindings=None):
    from ..workspace.input_resources import declared_resource_paths
    from ..workspace.async_protein_run import resolve_context_input_path
    if bindings is None:
        bindings = getattr(context, '_custom_mcp_read_bindings', [])
    used = []
    for parts, kind in declared_resource_paths(server, tool, arguments, context=context).items():
        value = arguments
        for part in parts:
            value = value[part]
        path = resolve_context_input_path(context, value)
        saved = next((b for b in bindings if b['argument_path'] == list(parts)), None)
        if (not saved or path is None or str(path) != saved['path'] or kind != saved['kind']
                or _read_input_identity(path, kind) != saved['identity']):
            raise ValueError('输入超出原读取授权或文件对象已变化，请重新申请；未读取或上传')
        used.append(saved)
    return used


@contextmanager
def _open_authorized_posix(grant, path, *, directory=False):
    root = Path(grant['path'])
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = Path(path.anchor)
        if current == root and file_identity(os.fstat(fd)) != grant['identity']:
            raise ValueError('已授权目录已变化，未读取；请重新申请')
        for index, part in enumerate(path.parts[1:]):
            last = index == len(path.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if last and not directory else os.O_DIRECTORY)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            current /= part
            if current == root and file_identity(os.fstat(fd)) != grant['identity']:
                raise ValueError('已授权文件或目录已变化，未读取；请重新申请')
        if not directory and not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('输入必须是普通文件，未读取')
        yield fd
    finally:
        os.close(fd)


def verify_authorized_directory(grant):
    """Verify the granted root even when the bundle contains no files."""
    path = Path(grant['path'])
    if _WINDOWS:
        if _read_input_identity(path, 'directory') != grant['identity']:
            raise ValueError('已授权目录已变化，未读取；请重新申请')
    else:
        with _open_authorized_posix(grant, path, directory=True):
            pass


@contextmanager
def open_authorized_input(grant, path):
    """Verify this parameter's grant on a pinned handle before reading any bytes."""
    path = Path(path)  # Already canonical at authorization; never resolve again.
    root = Path(grant['path'])
    directory = grant['kind'] == 'directory'
    if not (root in path.parents if directory else str(path) == grant['path']):
        raise ValueError('输入不在读取授权范围内，未读取')
    if _WINDOWS:
        _, open_file = _windows_input_io()
        with open_file(path, grant=(grant['path'], grant['identity'], directory)) as stream:
            yield stream
        return
    with _open_authorized_posix(grant, path) as fd, os.fdopen(os.dup(fd), 'rb') as stream:
        yield stream
