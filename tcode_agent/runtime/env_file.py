"""Deterministic loader for the shell-style env files used by TCode.

Only the small, declarative subset used by project configuration is parsed:
``source``/``.``, optional ``export``, quoted values, ``$VAR``/``${VAR}``, and
``${VAR:-default}``.  Shell commands are never executed.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import MutableMapping


_BRACED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\}")
_PLAIN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_BASH_SOURCE_DIR = re.compile(
    r'^\$\(cd "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)" && pwd\)/(.+)$'
)
_BASH_SOURCE_PARENT_DIR = '$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)'


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
        expanded = _PLAIN.sub(
            lambda match: str(env.get(match.group(1), "") or ""), expanded
        )
        if expanded == previous:
            return expanded
        previous = expanded
    return previous


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _source_path(value: str, *, env: MutableMapping[str, str], base: Path) -> Path | None:
    source_value = _unquote(value)
    bash_source_match = _BASH_SOURCE_DIR.fullmatch(source_value)
    if bash_source_match:
        # This exact expression is emitted by TCode's local config template.
        # Resolve it declaratively; never evaluate command substitutions.
        suffix = bash_source_match.group(1).strip()
        if not suffix or "$" in suffix or "`" in suffix:
            return None
        return resolve_env_path(suffix, base=base)
    if "$(" in source_value or "`" in source_value:
        return None
    return resolve_env_path(_expand(source_value, env), base=base)


def _assignment_value(
    value: str, *, env: MutableMapping[str, str], base: Path
) -> str | None:
    unquoted = _unquote(value)
    if unquoted == _BASH_SOURCE_PARENT_DIR:
        return str(base.parent.resolve(strict=False))
    if "$(" in unquoted or "`" in unquoted:
        return None
    return _expand(unquoted, env)


def load_env_file(
    env: MutableMapping[str, str],
    env_file: str | Path,
    *,
    protected_keys: set[str] | None = None,
    visited: set[Path] | None = None,
) -> None:
    """Load one env file and its includes without overriding process values."""

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
            directive = line[len("source ") :] if line.startswith("source ") else line[2:]
            raw_source_value = _unquote(directive.strip())
            if _BASH_SOURCE_DIR.fullmatch(raw_source_value):
                source_path = _source_path(
                    raw_source_value, env=env, base=path.parent
                )
            else:
                # Plain paths may contain shell-style quoting and comments.
                try:
                    tokens = shlex.split(line, comments=True, posix=True)
                except ValueError:
                    continue
                if len(tokens) < 2:
                    continue
                source_path = _source_path(tokens[1], env=env, base=path.parent)
            if source_path is not None:
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
        value = _assignment_value(raw_value, env=env, base=path.parent)
        if value is not None:
            env[key] = value


def load_process_env_file(env_file: str | Path) -> None:
    load_env_file(os.environ, env_file)
