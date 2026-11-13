"""Bounded file preflight shared by inspect_file and read."""

from __future__ import annotations

import codecs
import csv
import json
import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_MAGIC_TYPES: tuple[tuple[bytes, str, str], ...] = (
    (b"%PDF-", "application/pdf", "pdf"),
    (b"PK\x03\x04", "application/zip", "zip"),
    (b"\x1f\x8b", "application/gzip", "gzip"),
    (b"SQLite format 3\x00", "application/vnd.sqlite3", "sqlite"),
    (b"\x7fELF", "application/x-elf", "elf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpeg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)

_CODE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".md",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
}


def open_regular_file_no_symlinks(path: str | Path) -> tuple[int, os.stat_result]:
    """Open a regular file without following any path component symlink.

    Tool path policy resolves the requested path before this helper is called.
    Walking that canonical path one directory at a time closes the remaining
    check/open race where a writable parent could otherwise be swapped for a
    symlink after policy validation.
    """

    filepath = Path(path).expanduser()
    if not filepath.is_absolute():
        filepath = Path.cwd() / filepath
    filepath = filepath.resolve(strict=False)

    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    supports_dir_fd = os.open in getattr(os, "supports_dir_fd", set())
    if os.name != "posix" or not directory_flag or not supports_dir_fd:
        file_fd = os.open(filepath, file_flags)
    else:
        parts = filepath.parts
        if len(parts) < 2 or parts[0] != os.sep:
            raise ValueError("file path must resolve to an absolute file")
        directory_flags = (
            os.O_RDONLY
            | directory_flag
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_fd = os.open(os.sep, directory_flags)
        try:
            for component in parts[1:-1]:
                if component in {"", ".", ".."}:
                    raise ValueError("unsafe file path component")
                next_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            filename = parts[-1]
            if filename in {"", ".", ".."}:
                raise ValueError("file path must name a regular file")
            file_fd = os.open(filename, file_flags, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    info = os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(file_fd)
        raise ValueError("only regular files can be inspected or read")
    return file_fd, info


@dataclass(frozen=True)
class FileInspection:
    path: str
    size_bytes: int
    mtime_ns: int
    inode: int
    large_file: bool
    media_type: str
    encoding: str
    is_text: bool
    structure_kind: str
    delimiter: str
    oversized_line: bool
    sampled_head_bytes: int
    sampled_tail_bytes: int
    preview: str
    preview_truncated: bool
    recommended_tool: str
    recommendation: str

    @property
    def fingerprint(self) -> tuple[int, int, int]:
        return (self.inode, self.size_bytes, self.mtime_ns)

    def metadata(self, *, include_preview: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "tcode.file-inspection/v1",
            "path": self.path,
            "sizeBytes": self.size_bytes,
            "mtimeNs": self.mtime_ns,
            "inode": self.inode,
            "largeFile": self.large_file,
            "mediaType": self.media_type,
            "encoding": self.encoding,
            "isText": self.is_text,
            "structure": {
                "kind": self.structure_kind,
                "delimiter": self.delimiter or None,
                "oversizedLine": self.oversized_line,
            },
            "sample": {
                "headBytes": self.sampled_head_bytes,
                "tailBytes": self.sampled_tail_bytes,
                "previewChars": len(self.preview),
                "truncated": self.preview_truncated,
            },
            "recommendedAction": {
                "tool": self.recommended_tool,
                "reason": self.recommendation,
            },
        }
        if include_preview:
            payload["sample"]["preview"] = self.preview
        return payload


def _decode_sample(head: bytes) -> tuple[bool, str, str]:
    if not head:
        return True, "utf-8", ""
    if head.startswith(b"\xef\xbb\xbf"):
        try:
            decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
            return True, "utf-8-sig", decoder.decode(head, final=False)
        except UnicodeDecodeError:
            return False, "unknown", ""
    if head.startswith(b"\xff\xfe"):
        try:
            decoder = codecs.getincrementaldecoder("utf-16")(errors="strict")
            return True, "utf-16-le", decoder.decode(head, final=False)
        except UnicodeDecodeError:
            return False, "unknown", ""
    if head.startswith(b"\xfe\xff"):
        try:
            decoder = codecs.getincrementaldecoder("utf-16")(errors="strict")
            return True, "utf-16-be", decoder.decode(head, final=False)
        except UnicodeDecodeError:
            return False, "unknown", ""
    if b"\x00" in head:
        return False, "binary", ""
    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        return True, "utf-8", decoder.decode(head, final=False)
    except UnicodeDecodeError:
        return False, "unknown", ""


def _magic_type(head: bytes, tail: bytes, suffix: str) -> tuple[str, str]:
    for signature, media_type, kind in _MAGIC_TYPES:
        if head.startswith(signature):
            if kind == "zip" and suffix in {".docx", ".xlsx", ".pptx"}:
                office_type = mimetypes.guess_type(f"file{suffix}")[0]
                return office_type or media_type, suffix.lstrip(".")
            return media_type, kind
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    if head.startswith(b"PAR1") and tail.endswith(b"PAR1"):
        return "application/vnd.apache.parquet", "parquet"
    guessed = mimetypes.guess_type(f"file{suffix}")[0]
    return guessed or "application/octet-stream", "unknown"


def _json_lines(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines()[:12] if line.strip()]
    if len(lines) < 2:
        return False
    try:
        return all(isinstance(json.loads(line), (dict, list)) for line in lines)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _delimited_text(text: str) -> tuple[str, str]:
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    if len(lines) < 2:
        return "", ""
    sample = "\n".join(lines)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        return "", ""
    delimiter = str(getattr(dialect, "delimiter", "") or "")
    return ("tsv" if delimiter == "\t" else "csv"), delimiter


def _text_structure(text: str, suffix: str) -> tuple[str, str]:
    stripped = text.lstrip()
    if _json_lines(text):
        return "jsonl", ""
    if stripped.startswith(("{", "[")):
        return "json", ""
    delimited, delimiter = _delimited_text(text)
    if delimited:
        return delimited, delimiter
    upper_lines = [line[:16].upper() for line in text.splitlines()[:20]]
    if stripped.startswith(">"):
        return "fasta", ""
    if any(line.startswith(("ATOM", "HETATM", "HEADER")) for line in upper_lines):
        return "pdb", ""
    if suffix in {".yaml", ".yml"}:
        return "yaml", ""
    if suffix in _CODE_SUFFIXES:
        return "code", ""
    return "text", ""


def _recommendation(
    *,
    is_text: bool,
    kind: str,
    large_file: bool,
    oversized_line: bool,
) -> tuple[str, str]:
    if not is_text:
        mapping = {
            "pdf": ("pdf_parser", "PDF 应使用页面提取或 PDF 解析工具，不应按文本整文件读取。"),
            "zip": ("archive_parser", "压缩包应先列出成员并按需解包。"),
            "sqlite": ("sqlite", "SQLite 应通过只读查询检查表结构和目标记录。"),
            "parquet": ("parquet_parser", "Parquet 应先检查 schema，再按列或行组读取。"),
        }
        return mapping.get(
            kind,
            ("format_specific_parser", "二进制或非 UTF-8 文件应使用对应解析器。"),
        )
    if kind in {"jsonl", "csv", "tsv"} and large_file:
        return "grep_or_stream_parser", "结构化大文件应搜索目标或使用流式解析器汇总。"
    if kind == "json" and oversized_line:
        return "stream_parser", "单行大 JSON 应使用流式解析器或按列游标分页。"
    if large_file:
        return "grep_or_read", "先搜索目标内容，再用 offset/limit 读取相关窗口。"
    return "read", "文件较小，可使用有界 Read 读取。"


def inspect_file_bounded(
    path: str | Path,
    *,
    head_bytes: int = 16 * 1024,
    tail_bytes: int = 4 * 1024,
    preview_chars: int = 4 * 1024,
    large_file_bytes: int = 5 * 1024 * 1024,
) -> FileInspection:
    """Inspect one regular file without scanning or hashing the full content."""

    filepath = Path(path)
    head_limit = max(512, min(int(head_bytes), 256 * 1024))
    tail_limit = max(0, min(int(tail_bytes), 64 * 1024))
    preview_limit = max(256, min(int(preview_chars), 32 * 1024))

    file_fd, info = open_regular_file_no_symlinks(filepath)
    try:
        head = os.read(file_fd, head_limit)
        tail = b""
        if tail_limit and info.st_size > len(head):
            os.lseek(file_fd, max(0, info.st_size - tail_limit), os.SEEK_SET)
            tail = os.read(file_fd, tail_limit)
        final_info = os.fstat(file_fd)
        if (
            int(final_info.st_ino),
            int(final_info.st_size),
            int(final_info.st_mtime_ns),
        ) != (
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
        ):
            raise RuntimeError("file changed during bounded inspection; retry")
    finally:
        os.close(file_fd)

    suffix = filepath.suffix.lower()
    media_type, magic_kind = _magic_type(head, tail, suffix)
    is_text, encoding, decoded = _decode_sample(head)
    if magic_kind not in {"unknown"} and not media_type.startswith("text/"):
        is_text = False
        encoding = "binary"

    if is_text:
        structure_kind, delimiter = _text_structure(decoded, suffix)
        if media_type == "application/octet-stream":
            media_type = "text/plain"
    else:
        structure_kind, delimiter = magic_kind, ""

    oversized_line = bool(
        is_text
        and info.st_size > len(head)
        and "\n" not in decoded
        and "\r" not in decoded
    )
    preview = decoded[:preview_limit] if is_text else ""
    preview_truncated = bool(is_text and info.st_size > len(head)) or len(decoded) > preview_limit
    large_file = info.st_size >= max(1024, int(large_file_bytes))
    recommended_tool, recommendation = _recommendation(
        is_text=is_text,
        kind=structure_kind,
        large_file=large_file,
        oversized_line=oversized_line,
    )

    return FileInspection(
        path=filepath.as_posix(),
        size_bytes=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        inode=int(info.st_ino),
        large_file=large_file,
        media_type=media_type,
        encoding=encoding,
        is_text=is_text,
        structure_kind=structure_kind,
        delimiter=delimiter,
        oversized_line=oversized_line,
        sampled_head_bytes=len(head),
        sampled_tail_bytes=len(tail),
        preview=preview,
        preview_truncated=preview_truncated,
        recommended_tool=recommended_tool,
        recommendation=recommendation,
    )


__all__ = [
    "FileInspection",
    "inspect_file_bounded",
    "open_regular_file_no_symlinks",
]
