"""Small, deterministic loader for the project's shell-style env files.

It supports the subset used by TCode configs: ``source``/``.``, optional
``export``, quoted values, ``$VAR``/``${VAR}``, and ``${VAR:-default}``.
Existing process environment variables always win, while later assignments in
the loaded file may override values from a sourced base file.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import MutableMapping


_BRACED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\}")
_PLAIN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def resolve_env_path(value: str | Path, *, base: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(base).expanduser() / path
    return path.resolve(strict=False)


def _expand(value: str, env: MutableMapping[str, str]) -> str:
    def braced(match: re.Match[str]) -> str:
        name, operator, default = match.groups()
        current = str(env.get(name, "") or "")
        if operator == ":-":
            return current if current else str(default or "")
        if operator == "-":
            return current if name in env else str(default or "")
        return current

    previous = value
    for _ in range(8):
        expanded = _BRACED.sub(braced, previous)
        expanded = _PLAIN.sub(lambda match: str(env.get(match.group(1), "") or ""), expanded)
        if expanded == previous:
            return expanded
        previous = expanded
    return previous


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(
    env: MutableMapping[str, str],
    env_file: str | Path,
    *,
    protected_keys: set[str] | None = None,
    visited: set[Path] | None = None,
) -> None:
    path = Path(env_file).expanduser().resolve(strict=False)
    if not path.is_file():
        return
    protected = set(env) if protected_keys is None else protected_keys
    seen = set() if visited is None else visited
    if path in seen:
        return
    seen.add(path)

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("source ") or line.startswith(". "):
            try:
                tokens = shlex.split(line, comments=True, posix=True)
            except ValueError:
                continue
            if len(tokens) >= 2:
                source_value = _expand(tokens[1], env)
                source_path = resolve_env_path(source_value, base=path.parent)
                load_env_file(
                    env,
                    source_path,
                    protected_keys=protected,
                    visited=seen,
                )
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in protected:
            continue
        env[key] = _expand(_unquote(raw_value), env)


def load_process_env_file(env_file: str | Path) -> None:
    # Desktop already loaded configuration, then isolated its paths and ports.
    # Sidecars must inherit those values rather than restore the Web runtime.
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        return
    load_env_file(os.environ, env_file)
