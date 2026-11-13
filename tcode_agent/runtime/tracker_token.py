"""Shared Tracker service-token file handling for separately started processes."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Mapping, MutableMapping


TOKEN_ENV = "TCODE_TRACKER_API_TOKEN"
TOKEN_FILE_ENV = "TCODE_TRACKER_API_TOKEN_FILE"
DEFAULT_TOKEN_FILE = Path("tmp") / "tracker-api.token"


def resolve_tracker_token_file(
    environment: Mapping[str, str] | None = None,
) -> Path:
    env = environment if environment is not None else os.environ
    configured = str(env.get(TOKEN_FILE_ENV, "") or "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_TOKEN_FILE
    if not path.is_absolute():
        configured_root = str(
            env.get("TCODE_CONFIG_ROOT") or env.get("TCODE_WORKDIR") or ""
        ).strip()
        base = Path(configured_root).expanduser() if configured_root else Path.cwd()
        path = base / path
    return path.resolve(strict=False)


def _read_token_file(path: Path) -> str:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return ""
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError(f"tracker token path must be a regular file: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"tracker token file is empty: {path}")
    return token


def read_tracker_token(
    environment: Mapping[str, str] | None = None,
) -> str:
    env = environment if environment is not None else os.environ
    token = str(env.get(TOKEN_ENV, "") or "").strip()
    if token:
        return token
    return _read_token_file(resolve_tracker_token_file(env))


def _replace_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"tracker token path must not be a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_tracker_token(
    environment: MutableMapping[str, str] | None = None,
) -> str:
    env = environment if environment is not None else os.environ
    path = resolve_tracker_token_file(env)
    configured = str(env.get(TOKEN_ENV, "") or "").strip()
    if configured:
        existing = _read_token_file(path)
        if existing != configured:
            _replace_token_file(path, configured)
        token = configured
    else:
        token = _read_token_file(path)
        if not token:
            token = secrets.token_urlsafe(32)
            _replace_token_file(path, token)
    env[TOKEN_ENV] = token
    env[TOKEN_FILE_ENV] = path.as_posix()
    return token


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("ensure", "read"))
    args = parser.parse_args()
    token = ensure_tracker_token() if args.action == "ensure" else read_tracker_token()
    if not token:
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
