"""Resolve declared skill roots identically for catalog APIs and agents."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..config.manager import ConfigManager


def default_skill_roots(workdir: str, options: dict[str, Any] | None = None) -> list[Path]:
    options = options or {}
    base = Path(workdir or os.getcwd()).expanduser().resolve()
    definition = str(options.get('__definition_dir__') or options.get('definition_dir') or '').strip()
    anchor = Path(definition).expanduser().resolve() if definition else base
    raw = options.get('default_skill_roots') or options.get('default_skill_root') or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.replace('，', ',').split(',') if part.strip()]
    roots: list[Path] = []
    for value in raw if isinstance(raw, list) else []:
        if not str(value or '').strip():
            continue
        path = Path(str(value)).expanduser()
        roots.append((path if path.is_absolute() else anchor / path).resolve())
    if not roots:
        if str(os.environ.get('TCODE_CONFIG_ROOT') or '').strip():
            roots.append(Path(ConfigManager.get_effective_config_root(str(base))).expanduser().resolve())
        else:
            for candidate in dict.fromkeys([anchor, *anchor.parents, base, *base.parents]):
                if (candidate / 'tcode.json').exists() or (candidate / 'plugins.json').exists():
                    roots.append(candidate)
                    break
            if not roots:
                primary = ConfigManager.get_primary_config_path(str(base))
                if primary:
                    roots.append(Path(primary).expanduser().resolve().parent)
    return list(dict.fromkeys(path.parent if path.is_file() else path for path in roots))


def project_skill_roots() -> list[Path]:
    # Exact trusted project roots, not an unrestricted walk through workspace parents.
    roots = [Path(__file__).resolve().parents[2]]
    configured = str(os.environ.get('TCODE_PROJECT_ROOT') or '').strip()
    if configured:
        roots.append(Path(configured).expanduser().resolve())
    return list(dict.fromkeys(root for root in roots if root.is_dir()))
