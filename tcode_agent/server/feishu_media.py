"""Bounded, workspace-scoped Feishu attachments; never execute uploaded content."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 8
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
INPUT_SUFFIXES = IMAGE_SUFFIXES | {'.pdf', '.txt', '.md', '.csv', '.tsv', '.json', '.yaml', '.yml', '.xml', '.log', '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.sh', '.sql', '.r', '.go', '.java', '.c', '.cpp', '.h', '.toml', '.ini'}


def safe_name(value: str) -> str:
    name = re.sub(r'[^\w. -]', '_', Path(value.replace('\\', '/')).name)[:160].strip('. ')
    return name or 'attachment.bin'


def checked_bytes(path: Path) -> bytes:
    with path.open('rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('附件必须是普通文件')
        data = stream.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ValueError('附件为空或超过 10 MB 限制')
    return data


def save_input(workdir: str, session_id: str, message_id: str, name: str, data: bytes) -> str:
    if not data or len(data) > MAX_BYTES:
        raise ValueError('附件为空或超过 10 MB 限制')
    name = safe_name(name)
    if Path(name).suffix.lower() not in INPUT_SUFFIXES:
        raise ValueError('暂不支持此附件格式，请使用图片、PDF、文本或代码文件')
    # Use directory handles to reject symlink substitutions, including intermediate dirs.
    parts = ['upload', 'feishu', hashlib.sha256(session_id.encode()).hexdigest()[:24], hashlib.sha256(message_id.encode()).hexdigest()[:24]]
    fd = os.open(Path(workdir).resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        # A content digest makes redelivery stable without overwriting user changes.
        name = hashlib.sha256(data).hexdigest()[:16] + '-' + name
        try:
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        except FileExistsError:
            out = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(out, 'rb') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or f.read(MAX_BYTES + 1) != data:
                    raise ValueError('附件保存路径已有不同内容')
        else:
            with os.fdopen(out, 'wb') as f:
                f.write(data)
    finally:
        os.close(fd)
    return '/workspace/' + '/'.join([*parts, name])


def export_bytes(workdir: str, reference: str) -> tuple[str, bytes]:
    from .message_resources import _resolve_workspace_file_reference
    root = Path(workdir).resolve()
    path = _resolve_workspace_file_reference(reference, workdir=workdir)
    parts = path.relative_to(root).parts
    if any(p.startswith('.') and p != '.uploads' for p in parts):
        raise ValueError('不发送隐藏配置或凭据文件')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, 'rb') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('不能发送链接或非普通文件')
            data = f.read(MAX_BYTES + 1)
        if not data or len(data) > MAX_BYTES:
            raise ValueError('附件为空或超过 10 MB 限制')
        return path.name, data
    finally:
        os.close(fd)


def outputs(message: dict, session_id: str) -> list[dict]:
    """Only explicit final-answer links/presentation blocks are deliverables."""
    candidates = []
    for block in message.get('content_blocks') or []:
        if block.get('type') == 'image' and block.get('session_id') == session_id:
            candidates.append((block.get('path', ''), 'image'))
        elif block.get('type') == 'artifact':
            candidates.append((block.get('uri', ''), 'file'))
    for match in re.finditer(r'(!?)\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)\)', message.get('content') or ''):
        candidates.append((match[2].strip('<>'), 'image' if match[1] else 'file'))
    found = []
    seen = set()
    for reference, kind in candidates:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc:
            continue
        if parsed.path == '/api/files/download':
            query = parse_qs(parsed.query)
            if query.get('session_id', [session_id])[0] != session_id:
                continue
            reference = query.get('path', [''])[0]
        else:
            reference = unquote(parsed.path)
        if not reference or reference.startswith('/api/') or reference in seen:
            continue
        seen.add(reference)
        found.append({'path': reference, 'kind': kind})
    return found[:MAX_FILES]
