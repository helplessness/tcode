"""Lazy local-file to ArtifactRef conversion at remote MCP boundaries."""

from __future__ import annotations

import mimetypes
import os
import copy
from pathlib import Path
from typing import Any, Optional

import httpx

from ..tools.base import ToolContext
from ..workspace.async_protein_run import (
    _argument_value_may_be_path,
    resolve_context_input_path,
)
from ..workspace.input_resources import declared_resource_paths


_DEFAULT_PORTABLE_ARTIFACT_SERVERS = {
    "boltz-mcp",
    "evolvepro-mcp",
    "gnina-mcp",
    "ligandmpnn-mcp",
    "mutation-scoring-mcp",
    "placer-mcp",
    "plip-mcp",
    "rfdiffusion-mcp",
    "simulation-mcp",
    "pythia-mcp",
    "foldx-mcp",
    "homology-mcp",
}


class ArtifactBoundaryError(RuntimeError):
    """Raised when an MCP file input cannot be transported safely."""


def _directory_files(path: Path) -> list[Path]:
    return list(_directory_entries(path))


def _directory_entries(path: Path, *, _open_file=None) -> dict[Path, os.stat_result]:
    if os.name == 'nt' and path.is_junction():
        raise ArtifactBoundaryError('上传目录不能是 Windows 目录联接')
    if not path.is_dir():
        raise ArtifactBoundaryError('目录字段必须传入目录')
    files = {}
    total = 0
    max_bytes = int(os.environ.get('TCODE_PLUGIN_UPLOAD_MAX_BYTES', str(500 * 1024 * 1024)))
    max_files = int(os.environ.get('TCODE_PLUGIN_UPLOAD_MAX_FILES', '10000'))
    def onerror(error):
        raise error

    try:
        for root, directories, names in os.walk(path, onerror=onerror, followlinks=False):
            for name in directories + names:
                item = Path(root) / name
                if item.is_symlink():
                    raise ArtifactBoundaryError('上传目录不能包含符号链接')
                if os.name == 'nt' and item.is_junction():
                    raise ArtifactBoundaryError('上传目录不能包含 Windows 目录联接')
                if item.is_dir():
                    continue
                if not item.is_file():
                    raise ArtifactBoundaryError('上传目录包含不支持的文件类型')
                if _open_file:
                    with _open_file(item) as stream:
                        info = os.fstat(stream.fileno())
                else:
                    info = item.stat()
                total += info.st_size
                files[item] = info
                if len(files) > max_files or total > max_bytes:
                    raise ArtifactBoundaryError('目录超过上传文件数或总大小限制')
    except OSError as exc:
        raise ArtifactBoundaryError('无法检查上传目录内容，未开始上传') from exc
    return files


class _BoundedMember:
    """Keep a growing file from sending more than its enumerated size."""

    def __init__(self, stream, size):
        self.stream = stream
        self.remaining = size

    def fileno(self):
        return self.stream.fileno()

    def read(self, size=-1):
        # Look one byte past the boundary but never pass that byte to HTTPX.
        size = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        chunk = self.stream.read(size)
        if len(chunk) > self.remaining or (size and not chunk and self.remaining):
            raise ArtifactBoundaryError('目录成员大小在上传期间发生变化；未完成文件包')
        self.remaining -= len(chunk)
        return chunk


async def _upload_directory(path, workspace_id, *, _open_file=None, _check_directory=None, service_id=''):
    if _check_directory:
        _check_directory()
    files = _directory_entries(path, _open_file=_open_file)
    from .custom_targets import file_identity
    from ..artifact_transport import _artifact_endpoint
    selected, base, headers = _artifact_endpoint(service_id)
    async with httpx.AsyncClient(timeout=900, headers=headers) as client:
        if _check_directory:
            _check_directory()
        response = await client.post(base + '/api/v1/bundles', json={'name': path.name, 'workspace_id': workspace_id})
        response.raise_for_status()
        from ..artifact_services import bind_artifact_source
        identifier = bind_artifact_source(response.json(), selected)['bundle_id']
        for item, expected in files.items():
            with (_open_file(item) if _open_file else item.open('rb')) as stream:
                body = stream
                if _open_file:
                    if file_identity(os.fstat(stream.fileno())) != file_identity(expected):
                        raise ArtifactBoundaryError('目录成员在检查后发生变化；未完成文件包')
                    body = _BoundedMember(stream, expected.st_size)
                response = await client.put(f'{base}/api/v1/bundles/{identifier}/files',
                                            params={'relative_path': item.relative_to(path).as_posix()},
                                            files={'file': (item.name, body)})
                response.raise_for_status()
                if _open_file and (body.remaining or
                                   file_identity(os.fstat(stream.fileno())) != file_identity(expected)):
                    raise ArtifactBoundaryError('目录成员在上传期间发生变化；未完成文件包')
        if _check_directory:
            _check_directory()
        response = await client.post(f'{base}/api/v1/bundles/{identifier}/complete', json={'expected_files': len(files)})
        response.raise_for_status()
        bind_artifact_source(response.json(), selected)
    return {'kind': 'artifact_bundle', 'bundle_id': identifier, **({'service_id': selected} if selected else {})}


def _portable_artifact_servers() -> set[str]:
    configured = str(os.environ.get("TCODE_LAZY_ARTIFACT_MCP_SERVERS") or "").strip()
    if not configured:
        return set(_DEFAULT_PORTABLE_ARTIFACT_SERVERS)
    return {
        item.strip().lower()
        for item in configured.split(",")
        if item.strip()
    }


def _artifact_service_url() -> str:
    from ..artifact_transport import _artifact_service_url as service_url
    return service_url()


def _artifact_headers() -> dict[str, str]:
    token = str(
        os.environ.get("TMCP_ARTIFACT_API_TOKEN")
        or os.environ.get("TMCP_SERVICE_API_TOKEN")
        or ""
    ).strip()
    return {"X-Artifact-Token": token} if token else {}


def _context_workspace_id(context: Optional[ToolContext]) -> str:
    extra = getattr(context, "extra", None) if context is not None else None
    values = extra if isinstance(extra, dict) else {}
    return str(
        values.get("workspace_id")
        or values.get("workdir_name")
        or getattr(context, "session_id", "")
        or "local"
    ).strip()


def _nested_target_server(arguments: Any) -> str:
    if isinstance(arguments, dict):
        target = str(arguments.get("target_server") or "").strip().lower()
        if target:
            return target
        for key in ("input", "input_config", "payload", "arguments"):
            nested = arguments.get(key)
            target = _nested_target_server(nested)
            if target:
                return target
    return ""


def requires_lazy_artifact_transport(
    server_name: str,
    tool_name: str,
    arguments: Any,
) -> bool:
    server = str(server_name or "").strip().lower()
    if server in _portable_artifact_servers():
        return True
    if server == "job-protocol-mcp" and str(tool_name or "").strip() == "submit_job":
        return _nested_target_server(arguments) in _portable_artifact_servers()
    return False


async def upload_file_as_artifact(
    path: Path,
    *,
    workspace_id: str,
    _open_file=None,
    service_id: str = '',
) -> dict[str, str]:
    from ..artifact_transport import _artifact_endpoint
    selected, base, headers = _artifact_endpoint(service_id)
    timeout = float(os.environ.get("TMCP_ARTIFACT_PROXY_TIMEOUT_SECONDS", "900") or 900)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            with (_open_file(path) if _open_file else path.open("rb")) as stream:
                response = await client.post(
                    f"{base}/api/v1/artifacts",
                    data={"workspace_id": workspace_id},
                    files={"file": (path.name, stream, media_type)},
                    headers=headers,
                )
    except Exception as exc:
        raise ArtifactBoundaryError(
            f"无法在 MCP 调用前上传本地输入文件 {path.name}: {exc}"
        ) from exc
    if response.is_error:
        try:
            detail = str(response.json().get("detail") or "").strip()
        except Exception:
            detail = str(response.text or "").strip()
        raise ArtifactBoundaryError(
            f"MCP Artifact 服务拒绝文件 {path.name}: "
            f"HTTP {response.status_code}{f' - {detail}' if detail else ''}"
        )
    payload = response.json()
    from ..artifact_services import bind_artifact_source
    try:
        payload = bind_artifact_source(payload, selected)
    except ValueError as exc:
        raise ArtifactBoundaryError(str(exc)) from exc
    artifact_id = str(payload.get("artifact_id") or "").strip()
    if not artifact_id:
        raise ArtifactBoundaryError(
            f"MCP Artifact 服务没有为文件 {path.name} 返回 artifact_id"
        )
    return {"kind": "artifact", "artifact_id": artifact_id, **({'service_id': selected} if selected else {})}


async def prepare_mcp_artifact_arguments(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    context: Optional[ToolContext],
    *, saved_bindings: list[dict[str, Any]] | None = None, legacy_declarations: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Upload only local file values needed by this remote MCP invocation."""

    from .dynamic_headers import split, CONTEXT
    business, values = split(arguments)
    if values or CONTEXT in arguments:
        prepared, uploads = await prepare_mcp_artifact_arguments(
            server_name, tool_name, business, context,
            saved_bindings=saved_bindings, legacy_declarations=legacy_declarations)
        return {**prepared, CONTEXT: values}, uploads

    target_server = _nested_target_server(arguments) if server_name == 'job-protocol-mcp' else server_name
    if not target_server.startswith('plugin-') and not requires_lazy_artifact_transport(server_name, tool_name, arguments):
        return arguments, []

    workspace_id = _context_workspace_id(context)
    from .custom_targets import pinned_target, validate_read_bindings, open_authorized_input, verify_authorized_directory
    custom = pinned_target(context, target_server)
    read_bindings = {}
    try:
        if custom:
            used = validate_read_bindings(context, server_name, tool_name, arguments)
            read_bindings = {tuple(b['argument_path']): b for b in copy.deepcopy(used)}
            declared = {path: grant['kind'] for path, grant in read_bindings.items()}
        elif saved_bindings is not None:
            # A queued job's saved bindings are authoritative, even when a new
            # contract no longer names that field. Never discover extra inputs.
            declared = {}
            for binding in saved_bindings:
                path = binding.get("argument_path")
                if not isinstance(path, list) or not path or any(type(p) not in (str, int) for p in path):
                    raise ValueError("持久输入路径绑定无效，不能重新推断")
                value = arguments
                for part in path:
                    value = value[part]
                if isinstance(value, dict) and (value.get("artifact_id") or value.get("bundle_id")):
                    continue
                if not isinstance(value, str):
                    raise ValueError("持久输入路径绑定与参数不一致")
                declared[tuple(path)] = "file_or_directory"
        else:
            if not legacy_declarations:
                from ..workspace.input_resources import prepare_input_file_contract
                await prepare_input_file_contract(server_name, tool_name, arguments, context)
            declared = declared_resource_paths(server_name, tool_name, arguments, context=context, legacy=legacy_declarations)
    except ValueError as exc:
        raise ArtifactBoundaryError(str(exc)) from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise ArtifactBoundaryError("持久输入路径绑定与参数不一致，未上传；不能重新推断输入") from exc
    resolved_inputs: dict[tuple, Path] = {}
    for path, kind in (declared or {}).items():
        if custom:
            # Use the approved canonical path, not a second resolution of the user's alias.
            grant = read_bindings[path]
            if kind == 'directory':
                verify_authorized_directory(grant)
            resolved = Path(grant['path'])
        else:
            value: Any = arguments
            for part in path:
                value = value[part]
            resolved = resolve_context_input_path(context, value)
        if resolved is None:
            raise ArtifactBoundaryError(f"声明的输入文件不存在或不可访问：{'.'.join(map(str, path))}")
        if kind == "file" and not resolved.is_file():
            raise ArtifactBoundaryError(f"输入文件字段不能传入目录：{'.'.join(map(str, path))}")
        if kind == "directory" and not resolved.is_dir():
            raise ArtifactBoundaryError(f"输入目录字段不能传入文件：{'.'.join(map(str, path))}")
        resolved_inputs[path] = resolved
    # Validate every directory before uploading even the first file/bundle.
    # Keep upload-time validation too: the filesystem may change afterwards.
    for directory in {resolved for path, resolved in resolved_inputs.items()
                      if (declared[path] == 'directory' if custom else resolved.is_dir())}:
        _directory_files(directory)
    uploaded_by_path: dict[str | tuple, dict[str, str]] = {}
    records: list[dict[str, Any]] = []

    async def _walk(value: Any, key: str = "", path: tuple = ()) -> Any:
        if declared is not None and not any(item[:len(path)] == path for item in declared):
            return copy.deepcopy(value)
        if isinstance(value, dict):
            if (
                str(value.get("kind") or "").strip().lower() == "artifact"
                and str(value.get("artifact_id") or "").strip()
            ):
                return dict(value)
            if str(value.get("kind") or "").strip().lower() == "artifact":
                ref_path = value.get("path")
                if not isinstance(ref_path, str) or not ref_path.strip():
                    raise ArtifactBoundaryError("文件引用缺少 artifact_id 或有效的工作区 path")
                if declared is None:
                    resolved = resolve_context_input_path(context, ref_path)
                    if resolved is None or not resolved.is_file():
                        raise ArtifactBoundaryError("文件引用指向的工作区文件不存在或不可访问")
                return await _walk(ref_path, "file", (*path, "path"))
            return {
                nested_key: await _walk(nested_value, str(nested_key), (*path, nested_key))
                for nested_key, nested_value in value.items()
            }
        if isinstance(value, list):
            return [await _walk(item, key, (*path, index)) for index, item in enumerate(value)]
        if not isinstance(value, str):
            return value
        if declared is not None:
            if path not in declared:
                return value
        elif not _argument_value_may_be_path(value, key):
            return value
        resolved = resolved_inputs[path] if declared is not None else resolve_context_input_path(context, value)
        if resolved is None or (declared is None and not resolved.is_file()):
            return value
        grant = read_bindings.get(path)
        if grant:
            cache_key = (grant['path'], grant['kind'], tuple(grant['identity']))
        else:
            cache_key = resolved.resolve(strict=True).as_posix()
        artifact_ref = uploaded_by_path.get(cache_key)
        if artifact_ref is None:
            if context is not None:
                await context.report_progress(
                    {
                        "message": f"正在为 MCP 上传输入文件：{resolved.name}",
                        "stream": "mcp",
                        "status": "running",
                    }
                )
            options = {}
            directory = grant['kind'] == 'directory' if grant else resolved.is_dir()
            if grant:
                options['_open_file'] = lambda item: open_authorized_input(grant, item)
                if directory:
                    options['_check_directory'] = lambda: verify_authorized_directory(grant)
            from ..artifact_transport import _artifact_endpoint
            service_id, _, _ = _artifact_endpoint(target_server=target_server)
            if service_id:
                options['service_id'] = service_id
            artifact_ref = (await _upload_directory(resolved, workspace_id, **options) if directory
                            else await upload_file_as_artifact(resolved, workspace_id=workspace_id, **options))
            uploaded_by_path[cache_key] = artifact_ref
            records.append(
                {
                    "workspace_path": value,
                    "artifact_ref": dict(artifact_ref),
                    "size": resolved.stat().st_size,
                }
            )
        return dict(artifact_ref)

    prepared = await _walk(dict(arguments or {}))
    return prepared, records
