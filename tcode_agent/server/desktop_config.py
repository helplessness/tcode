"""Desktop service configuration never falls back to a developer's local MCPs."""
from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


DESKTOP_PERMISSION_MODES = frozenset({"ask", "auto"})


def remote_services_paused() -> bool:
    """Pause service traffic, not the native plugin isolation backend."""
    return os.environ.get('TCODE_DESKTOP_MANAGED') == '1' and os.environ.get('TCODE_DESKTOP_PAUSE_REMOTE_SERVICES') == '1'


def desktop_permission_mode(value: object) -> str:
    """Normalize the desktop-only approval UX without changing policy rules."""
    normalized = str(value or "ask").strip().lower()
    return normalized if normalized in DESKTOP_PERMISSION_MODES else "ask"


def remote_url(value: str, *, allow_loopback: bool = False) -> bool:
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        url.port  # Validate malformed ports too.
        if url.scheme not in {"http", "https"} or not host or url.username or url.password or url.query or url.fragment:
            return False
        if host == "localhost" or host.endswith(".localhost"):
            return allow_loopback
        try:
            address = ipaddress.ip_address(host)
            address = getattr(address, "ipv4_mapped", None) or address
            return not (address.is_unspecified or address.is_link_local) and (allow_loopback or not address.is_loopback)
        except ValueError:
            return True
    except (ValueError, TypeError):
        return False


def local_development_services() -> bool:
    return (os.environ.get("TCODE_DESKTOP_MANAGED") == "1"
            and os.environ.get("TCODE_DESKTOP_DEV_LOCAL_SERVICES") == "1")


def artifact_configuration(entry: dict, *, allow_loopback: bool | None = None) -> dict:
    """Editable endpoint metadata only; never return credential values."""
    if not isinstance(entry, dict):
        raise ValueError('Artifact 配置必须是对象')
    services = entry.get('artifact_services')
    default = entry.get('artifact_default_service_id', os.environ.get('TMCP_ARTIFACT_SERVICE_ID', ''))
    routes = entry.get('artifact_target_services')
    if services is None:
        services = json.loads(os.environ.get('TMCP_ARTIFACT_SERVICES') or '{}')
    if routes is None:
        routes = json.loads(os.environ.get('TMCP_ARTIFACT_TARGET_SERVICES') or '{}')
    if not isinstance(services, dict) or len(services) > 64:
        raise ValueError('Artifact 服务配置须为对象，最多 64 项')
    legacy = str(entry.get('artifact_service_url') or os.environ.get('TCODE_DESKTOP_ARTIFACT_SERVICE_URL', '') or
                 (os.environ.get('TMCP_ARTIFACT_SERVICE_URL', '') if os.environ.get('TCODE_DESKTOP_MANAGED') != '1' else ''))
    if not services and legacy and 'artifact_services' not in entry:
        default = default or 'default'
        token_env = next((key for key in ('TMCP_ARTIFACT_API_TOKEN', 'TMCP_SERVICE_API_TOKEN') if os.environ.get(key)), '')
        services = {default: {'url': legacy, 'token_env': token_env}}
    cleaned = {}
    for key, service in services.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', key) or not isinstance(service, dict):
            raise ValueError('Artifact 服务 ID 无效（使用 1–64 位字母、数字、点、下划线或连字符）')
        url = service.get('url', '')
        token_env = service.get('token_env', '')
        name = service.get('name', key)
        if not isinstance(url, str) or not remote_url(url, allow_loopback=local_development_services() if allow_loopback is None else allow_loopback):
            raise ValueError(f'Artifact 服务 {key} 地址必须是有效的远程 HTTP(S) URL')
        if not isinstance(token_env, str) or (token_env and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', token_env)):
            raise ValueError(f'Artifact 服务 {key} 的凭据环境变量名无效')
        if not isinstance(name, str) or len(name) > 120:
            raise ValueError('Artifact 服务名称过长或无效')
        cleaned[key] = {'url': url.rstrip('/'), 'name': name, 'token_env': token_env}
    if not isinstance(default, str) or (services and default not in services) or (not services and default):
        raise ValueError('请选择已配置的默认 Artifact 服务')
    if not isinstance(routes, dict) or any(
        not isinstance(target, str) or not target.strip() or not isinstance(service, str) or service not in cleaned
        for target, service in routes.items()
    ):
        raise ValueError('MCP 绑定必须指向已配置的 Artifact 服务')
    return {'artifact_services': cleaned, 'artifact_default_service_id': default,
            'artifact_target_services': routes}


def desktop_mcp_config_path(project: Path) -> Path:
    config_root = Path(os.environ.get("TCODE_CONFIG_ROOT") or project / ".tcode-local").expanduser()
    source = Path(os.environ.get("TCODE_DESKTOP_MCP_CONFIG") or config_root / "desktop-mcp.json").expanduser()
    return source if source.is_absolute() else project / source


def prepare_services(project: Path, runtime: Path, *, managed_job_url: str = "") -> None:
    source = desktop_mcp_config_path(project)
    source.parent.mkdir(parents=True, exist_ok=True)
    template = {"$desktop": {"artifact_service_url": "", "managed_job_protocol": bool(managed_job_url)},
                "job-protocol-mcp": {"url": managed_job_url, "transport": "streamable-http", "enabled": bool(managed_job_url),
                                     "connect_timeout": 10000, "timeout": 300000}}
    try:
        with source.open("x", encoding="utf-8") as handle:
            os.chmod(source, 0o600)
            json.dump(template, handle, ensure_ascii=False, indent=2)
    except FileExistsError:
        pass
    state = "unconfigured"
    config = {}
    candidate = {}
    try:
        candidate = json.loads(source.read_text())
        if not isinstance(candidate, dict):
            raise ValueError("expected server mapping")
        desktop_entry = candidate.get("$desktop", {})
        job_entry = candidate.get("job-protocol-mcp", {})
        desktop_managed = isinstance(desktop_entry, dict) and bool(desktop_entry.get("managed_job_protocol"))
        # Legacy desktop files had a loopback URL but no ownership marker.
        # Migrate those to the managed gateway; explicit false remains external.
        legacy_local_job = (
            isinstance(desktop_entry, dict) and "managed_job_protocol" not in desktop_entry
            and isinstance(job_entry, dict)
            and urlsplit(str(job_entry.get("url") or "")).hostname in {"127.0.0.1", "localhost", "::1"}
        )
        use_managed_job = bool(managed_job_url) and (
            not isinstance(job_entry, dict)
            or not str(job_entry.get("url") or "").strip()
            or desktop_managed
            or legacy_local_job
        )
        if use_managed_job and not remote_services_paused():
            candidate["job-protocol-mcp"] = {
                "url": managed_job_url, "transport": "streamable-http", "enabled": True,
                "connect_timeout": 10000, "timeout": 300000,
            }
            candidate["$desktop"] = {**(desktop_entry if isinstance(desktop_entry, dict) else {}), "managed_job_protocol": True}
            temp_source = source.with_suffix(source.suffix + ".tmp")
            temp_source.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(temp_source, 0o600)
            temp_source.replace(source)
        elif (desktop_managed or legacy_local_job) and not managed_job_url:
            # Do not publish a stale address or erase ownership needed on restart.
            candidate.pop("job-protocol-mcp", None)
        for name, entry in candidate.items():
            if remote_services_paused():
                continue
            if str(name).startswith("$"):
                continue
            if not isinstance(entry, dict):
                raise ValueError("invalid server entry")
            if entry.get("enabled", True) is False:
                continue
            allow_managed = name == "job-protocol-mcp" and entry.get("url") == managed_job_url
            allow_managed = allow_managed or (
                name == "blender-mcp" and bool(os.environ.get("TCODE_DESKTOP_BLENDER_MCP_URL"))
                and entry.get("url") == os.environ["TCODE_DESKTOP_BLENDER_MCP_URL"]
            )
            if not remote_url(entry.get("url", ""), allow_loopback=local_development_services() or allow_managed) or entry.get("transport", "streamable-http") not in {"streamable-http", "sse"}:
                raise ValueError("desktop MCP requires remote HTTP endpoint")
            config[name] = entry
        state = "configured" if "job-protocol-mcp" in config else "unconfigured"
    except (OSError, ValueError, TypeError):
        state, config = "invalid", {}
    target = runtime / ".tcode/desktop-mcp.effective.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        json.dump(config, handle, ensure_ascii=False, indent=2)
    temp.replace(target)
    os.environ.update(TCODE_DESKTOP_MCP_CONFIG=str(source), TCODE_MCP_CONFIG=str(target),
                      TCODE_BRIDGE_MCP_CONFIG=str(target), TCODE_DESKTOP_REMOTE_STATE=state,
                      TCODE_MCP_PROFILE="job-protocol-only", TCODE_PROXY_SUBMIT_MODE="bridge",
                      TCODE_ASYNC_EXECUTION_MODE="bridge", SYMPHONY_ROUTE_ON_MCP_TOOL="false")
    # No local Runner, Docker connection, or server isolation policy is inherited.
    for key in list(os.environ):
        if key.startswith("TCODE_SANDBOX_") or key in {"DOCKER_HOST", "DOCKER_CONTEXT"}:
            os.environ.pop(key, None)
    os.environ.update(TCODE_ISOLATION_MODE="development", TCODE_SANDBOX_ENABLED="true",
                      TCODE_SANDBOX_MODE="runtime", TCODE_SANDBOX_BACKEND="desktop-unavailable",
                      TCODE_SANDBOX_REQUIRE_RUNNER="false")
    desktop_entry = candidate.get("$desktop", {}) if isinstance(candidate, dict) else {}
    if isinstance(desktop_entry, dict) and 'artifact_services' in desktop_entry:
        try:
            artifacts = artifact_configuration(desktop_entry)
        except (ValueError, TypeError):
            # Fail closed rather than silently retaining endpoints from a prior launch.
            artifacts = {'artifact_services': {}, 'artifact_default_service_id': '', 'artifact_target_services': {}}
            desktop_entry = {**desktop_entry, 'artifact_service_url': ''}
            os.environ['TCODE_DESKTOP_ARTIFACT_SERVICE_URL'] = ''
        os.environ.update(TMCP_ARTIFACT_SERVICES=json.dumps(artifacts['artifact_services']),
                          TMCP_ARTIFACT_SERVICE_ID=artifacts['artifact_default_service_id'],
                          TMCP_ARTIFACT_TARGET_SERVICES=json.dumps(artifacts['artifact_target_services']))
        if artifacts['artifact_default_service_id']:
            desktop_entry = {**desktop_entry, 'artifact_service_url': artifacts['artifact_services'][artifacts['artifact_default_service_id']]['url']}
    configured_artifact = str(desktop_entry.get("artifact_service_url") or os.environ.get("TCODE_DESKTOP_ARTIFACT_SERVICE_URL", "")) if isinstance(desktop_entry, dict) else os.environ.get("TCODE_DESKTOP_ARTIFACT_SERVICE_URL", "")
    if remote_services_paused():
        configured_artifact = ''
    os.environ["TCODE_DESKTOP_ARTIFACT_SERVICE_URL"] = configured_artifact
    os.environ["TMCP_ARTIFACT_SERVICE_URL"] = configured_artifact
    from ..runtime.sandbox.macos_native import probe
    os.environ["TCODE_DESKTOP_NATIVE_READY"] = "1" if probe() else "0"
    if os.environ["TCODE_DESKTOP_NATIVE_READY"] == "1":
        os.environ["TCODE_SANDBOX_BACKEND"] = "macos-seatbelt"
        os.environ["TCODE_PLUGIN_ISOLATION_BACKEND"] = "native"
        os.environ["TCODE_PLUGIN_RUNTIME_ROOT"] = str(
            runtime / ".tcode/plugin-workers"
        )
    else:
        os.environ["TCODE_PLUGIN_ISOLATION_BACKEND"] = "unavailable"
