"""外部 CLI 可执行文件、工作区和环境边界。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


DEFAULT_ENV_ALLOWLIST = {
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
}


def minimal_environment(
    source: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = source or os.environ
    configured = {
        item.strip()
        for item in str(os.environ.get("TCODE_HARNESS_ENV_ALLOWLIST") or "").split(",")
        if item.strip()
    }
    allowed = DEFAULT_ENV_ALLOWLIST | configured
    result = {key: str(source[key]) for key in allowed if key in source}
    result.setdefault("PATH", os.defpath)
    result["TCODE_EXTERNAL_HARNESS"] = "1"
    platform_root = Path(__file__).resolve().parents[2].as_posix()
    existing_pythonpath = result.get("PYTHONPATH", "")
    pythonpath_parts = [platform_root]
    if existing_pythonpath:
        pythonpath_parts.extend(
            part for part in existing_pythonpath.split(os.pathsep) if part and part != platform_root
        )
    result["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    result["TCODE_PLATFORM_ROOT"] = platform_root
    result["TCODE_PYTHON"] = sys.executable
    for key, value in dict(extra or {}).items():
        if str(key).startswith("TCODE_PLATFORM_") and str(value):
            result[str(key)] = str(value)
    return result


def assert_workspace(workdir: str) -> str:
    raw = Path(str(workdir or "")).expanduser()
    if not raw.is_absolute():
        raise ValueError("harness workspace must be absolute")
    resolved = raw.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("harness workspace is not a directory")
    return resolved.as_posix()


def render_argv(values: list[str], *, workspace: str, model_id: str = "") -> list[str]:
    rendered: list[str] = []
    for raw in values:
        value = str(raw).replace("${TCODE_RESOLVED_WORKSPACE}", workspace)
        value = value.replace("${TCODE_MODEL_ID}", model_id)
        if "${" in value:
            raise ValueError("unknown harness argv placeholder")
        rendered.append(value)
    return rendered


__all__ = ["assert_workspace", "minimal_environment", "render_argv"]
