"""Validation and persistence-safe metadata for frontend image blocks."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .job_results import normalize_job_result_block


SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
}
_SUFFIX_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
_ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,96}$")


def _max_image_bytes() -> int:
    default = 25 * 1024 * 1024
    try:
        configured = int(os.environ.get("TCODE_PRESENT_IMAGE_MAX_BYTES", default))
    except Exception:
        configured = default
    return max(1024, min(configured, 250 * 1024 * 1024))


def _detect_image_mime(header: bytes) -> str:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return ""


def inspect_image_file(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_file():
        raise ValueError(f"Image file not found: {target}")

    size = int(target.stat().st_size)
    if size <= 0:
        raise ValueError(f"Image file is empty: {target}")
    maximum = _max_image_bytes()
    if size > maximum:
        raise ValueError(
            f"Image file is too large for inline display: {size} bytes (max {maximum})"
        )

    expected_mime = _SUFFIX_MIME_TYPES.get(target.suffix.lower(), "")
    if not expected_mime:
        raise ValueError(
            "Unsupported image type. Supported: PNG, JPG, JPEG, WEBP, GIF, BMP"
        )
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        header = handle.read(16)
        digest.update(header)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    detected_mime = _detect_image_mime(header)
    if not detected_mime:
        raise ValueError(f"File content is not a supported image: {target}")
    if detected_mime != expected_mime:
        raise ValueError(
            f"Image extension/content mismatch: extension={expected_mime}, content={detected_mime}"
        )
    return {
        "path": target,
        "mime_type": detected_mime,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def make_presented_image_block(
    *,
    session_id: str,
    relative_path: str,
    mime_type: str,
    size: int,
    sha256: str = "",
    alt: str = "",
    caption: str = "",
) -> dict[str, Any]:
    normalized_session_id = str(session_id or "").strip()
    normalized_path = str(relative_path or "").strip().lstrip("/")
    if not normalized_session_id or not normalized_path:
        raise ValueError("session_id and workspace-relative image path are required")
    normalized_mime = str(mime_type or "").strip().lower()
    if normalized_mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ValueError(f"Unsupported image MIME type: {normalized_mime}")

    normalized_sha256 = str(sha256 or "").strip().lower()
    if normalized_sha256 and not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
        raise ValueError("Invalid image SHA-256 digest")

    identity = f"{normalized_session_id}\0{normalized_path}\0{normalized_sha256}\0{int(size or 0)}".encode(
        "utf-8"
    )
    asset_id = "img_" + hashlib.sha256(identity).hexdigest()[:24]
    return {
        "type": "image",
        "asset_id": asset_id,
        "session_id": normalized_session_id,
        "path": normalized_path,
        "mime_type": normalized_mime,
        "size": max(0, int(size or 0)),
        "sha256": normalized_sha256,
        "alt": str(alt or "").strip()[:500] or Path(normalized_path).name,
        "caption": str(caption or "").strip()[:2000],
    }


def normalize_content_blocks(value: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "").strip()
        if block_type == "intelligent_page":
            from .intelligent_page import normalize_page_block
            page = normalize_page_block(item)
            if page:
                key = f"page:{page['page_id']}:{page['revision_id']}"
                if key not in seen:
                    seen.add(key)
                    blocks.append(page)
            continue
        if block_type == "canvas":
            from .canvas import normalize_canvas_block
            canvas = normalize_canvas_block(item)
            if canvas:
                key = f"canvas:{canvas['canvas_id']}:{canvas['revision_id']}"
                if key not in seen:
                    seen.add(key)
                    blocks.append(canvas)
            continue
        if block_type == "result_selection":
            from .result_selection import normalize_selection
            selection = normalize_selection(item)
            if selection:
                blocks.append(selection)
            continue
        if block_type == "research_compare":
            from .research_compare import normalize_compare
            comparison = normalize_compare(item)
            if comparison:
                key = f"compare:{comparison['run_id']}:{comparison['result_digest']}:{comparison['session_id']}"
                if key not in seen:
                    seen.add(key)
                    blocks.append(comparison)
            continue
        if block_type in {"job_summary", "data_table"}:
            result_block = normalize_job_result_block(item)
            if result_block is None:
                continue
            identity = str(
                result_block.get("job_id")
                or result_block.get("id")
                or result_block.get("title")
                or ""
            )
            key = f"{block_type}:{identity}"
            if key in seen:
                continue
            seen.add(key)
            blocks.append(result_block)
            continue
        if block_type == "artifact":
            artifact_id = str(item.get("artifact_id") or "").strip()
            uri = str(item.get("uri") or "").strip()
            media_type = str(item.get("media_type") or "application/octet-stream").strip()
            if (
                not re.fullmatch(r"art_[0-9a-f]{20,64}", artifact_id)
                or not uri.startswith("/api/files/download?")
                or not media_type
            ):
                continue
            key = f"artifact:{artifact_id}"
            if key in seen:
                continue
            seen.add(key)
            blocks.append(
                {
                    "type": "artifact",
                    "artifact_id": artifact_id,
                    "uri": uri[:4096],
                    "media_type": media_type[:200],
                    "title": str(item.get("title") or "Artifact")[:500],
                    "fallback": str(item.get("fallback") or "Artifact")[:20_000],
                }
            )
            continue
        if block_type == "mcp_app":
            session_id = str(item.get("session_id") or "").strip()
            server_name = str(item.get("server_name") or "").strip()
            tool_name = str(item.get("tool_name") or "").strip()
            resource_uri = str(item.get("resource_uri") or "").strip()
            parsed = urlparse(resource_uri)
            if (
                not session_id
                or not server_name
                or not tool_name
                or parsed.scheme != "ui"
            ):
                continue
            computed_app_id = "app_" + hashlib.sha256(
                f"{server_name}\0{tool_name}\0{resource_uri}".encode("utf-8")
            ).hexdigest()[:24]
            app_id = str(item.get("app_id") or computed_app_id).strip()
            if not re.fullmatch(r"app_[0-9a-f]{24}", app_id):
                continue
            key = f"mcp_app:{session_id}:{app_id}:{resource_uri}"
            if key in seen:
                continue
            seen.add(key)
            block = {
                "type": "mcp_app",
                "schema": "tcode.mcp-app.v2",
                "app_id": app_id,
                "session_id": session_id[:256],
                "server_name": server_name[:200],
                "tool_name": tool_name[:300],
                "resource_uri": resource_uri[:2000],
                "mime_type": "text/html;profile=mcp-app",
                "tool_input": (
                    dict(item.get("tool_input") or {})
                    if isinstance(item.get("tool_input"), dict)
                    else {}
                ),
                "tool_result": (
                    dict(item.get("tool_result") or {})
                    if isinstance(item.get("tool_result"), dict)
                    else {"content": []}
                ),
                "fallback": str(item.get("fallback") or "交互界面暂不可用")[:2000],
            }
            blocks.append(block)
            continue
        if block_type != "image":
            continue
        asset_id = str(item.get("asset_id") or "").strip()
        session_id = str(item.get("session_id") or "").strip()
        path = str(item.get("path") or "").strip().lstrip("/")
        mime_type = str(item.get("mime_type") or "").strip().lower()
        if (
            not _ASSET_ID_PATTERN.fullmatch(asset_id)
            or not session_id
            or not path
            or mime_type not in SUPPORTED_IMAGE_MIME_TYPES
        ):
            continue
        key = f"{session_id}:{asset_id}"
        if key in seen:
            continue
        seen.add(key)
        try:
            size = max(0, int(item.get("size") or 0))
        except Exception:
            size = 0
        sha256 = str(item.get("sha256") or "").strip().lower()
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            sha256 = ""
        normalized_block = {
            "type": "image",
            "asset_id": asset_id,
            "session_id": session_id[:256],
            "path": path[:4096],
            "mime_type": mime_type,
            "size": size,
            "alt": str(item.get("alt") or "").strip()[:500]
            or Path(path).name,
            "caption": str(item.get("caption") or "").strip()[:2000],
        }
        if sha256:
            normalized_block["sha256"] = sha256
        blocks.append(normalized_block)
    return blocks


def collect_presented_image_blocks(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """收集前端可展示内容；保留旧函数名以兼容现有调用方。"""

    candidates: list[dict[str, Any]] = []
    for message in messages or []:
        candidates.extend(normalize_content_blocks(getattr(message, "content_blocks", [])))
        for tool_call in getattr(message, "tool_calls", []) or []:
            metadata = dict(getattr(tool_call, "metadata", {}) or {})
            block = metadata.get("content_block")
            if isinstance(block, dict):
                candidates.append(block)
            mcp_app = metadata.get("mcp_app_descriptor")
            if isinstance(mcp_app, dict):
                candidates.append(mcp_app)
    return normalize_content_blocks(candidates)


def find_presented_image_block(messages: Iterable[Any], asset_id: str) -> dict[str, Any] | None:
    normalized_asset_id = str(asset_id or "").strip()
    for message in reversed(list(messages or [])):
        for block in normalize_content_blocks(getattr(message, "content_blocks", [])):
            if block.get("asset_id") == normalized_asset_id:
                return block
    return None
