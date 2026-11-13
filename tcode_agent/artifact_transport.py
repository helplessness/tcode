"""Materialize portable Artifact service references into an Agent workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


_PORTABLE_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


class ArtifactMaterializationError(RuntimeError):
    """Raised when a portable output reference cannot be safely materialized."""


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(str(os.environ.get(name, default)).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _artifact_endpoint(service_id: str = '', *, target_server: str = '') -> tuple[str, str, dict[str, str]]:
    from .artifact_services import artifact_service
    token = os.environ.get('TMCP_ARTIFACT_API_TOKEN') or os.environ.get('TMCP_SERVICE_API_TOKEN') or ''
    try:
        selected, url, headers = artifact_service(
            service_id, target_server=target_server,
            default_url=os.environ.get('TMCP_ARTIFACT_SERVICE_URL', 'http://127.0.0.1:10120'),
            default_token=token,
        )
    except ValueError as exc:
        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1':
            raise ArtifactMaterializationError('请检查 Artifact 服务配置或 TCODE_DESKTOP_ARTIFACT_SERVICE_URL') from exc
        raise
    if os.environ.get("TCODE_DESKTOP_MANAGED") == "1":
        from .server.desktop_config import local_development_services, remote_services_paused, remote_url
        if remote_services_paused() or not remote_url(url, allow_loopback=local_development_services()):
            raise ArtifactMaterializationError("请在桌面配置中设置远程文件服务 TCODE_DESKTOP_ARTIFACT_SERVICE_URL")
    return selected, url, headers


def _artifact_service_url(service_id: str = '') -> str:
    return _artifact_endpoint(service_id)[1]


def _artifact_headers(service_id: str = '') -> dict[str, str]:
    return _artifact_endpoint(service_id)[2]


def _request_timeout() -> float:
    raw = str(
        os.environ.get("TCODE_ARTIFACT_DOWNLOAD_TIMEOUT_SECONDS")
        or os.environ.get("TMCP_ARTIFACT_PROXY_TIMEOUT_SECONDS")
        or "900"
    ).strip()
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 900.0


def _response_error(exc: Exception) -> ArtifactMaterializationError:
    if isinstance(exc, HTTPError):
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace").strip()
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        return ArtifactMaterializationError(
            f"Artifact service returned HTTP {exc.code}{suffix}"
        )
    if isinstance(exc, URLError):
        return ArtifactMaterializationError(
            f"Artifact service request failed: {exc.reason}"
        )
    return ArtifactMaterializationError(f"Artifact service request failed: {exc}")


def _request_json(url: str, *, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    request = Request(url, headers=_artifact_headers() if headers is None else headers, method="GET")
    token = request.headers.pop('X-artifact-token', None)
    if token:
        request.add_unredirected_header('X-Artifact-Token', token)
    try:
        with urlopen(request, timeout=_request_timeout()) as response:
            limit = _env_int('TCODE_ARTIFACT_MANIFEST_MAX_BYTES', 16 * 1024 * 1024)
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ArtifactMaterializationError('Artifact manifest exceeds the configured size limit')
            payload = json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise _response_error(exc) from exc
    if not isinstance(payload, dict):
        raise ArtifactMaterializationError(
            "Artifact service returned a non-object response"
        )
    return payload


def _valid_bundle_ref(value: Any) -> Optional[dict[str, str]]:
    if not isinstance(value, dict):
        return None
    bundle_id = str(value.get("bundle_id") or "").strip()
    kind = str(value.get("kind") or "artifact_bundle").strip().lower()
    if kind not in {"artifact_bundle", "bundle"} or not _PORTABLE_ID.fullmatch(
        bundle_id
    ):
        return None
    ref = {"kind": "artifact_bundle", "bundle_id": bundle_id}
    if value.get('service_id'):
        ref['service_id'] = value['service_id']
    return ref


def find_output_bundle(value: Any) -> Optional[dict[str, str]]:
    """Find an ``output_bundle`` in plain or MCP-wrapped result data."""

    direct_value = _valid_bundle_ref(value)
    if direct_value:
        return direct_value
    seen: set[int] = set()

    def _walk(item: Any, depth: int = 0) -> Optional[dict[str, str]]:
        if depth > 24:
            return None
        if isinstance(item, str):
            text = item.strip()
            if not text or text[:1] not in {"{", "["}:
                return None
            try:
                return _walk(json.loads(text), depth + 1)
            except (TypeError, ValueError):
                return None
        if not isinstance(item, (dict, list)):
            return None
        marker = id(item)
        if marker in seen:
            return None
        seen.add(marker)
        if isinstance(item, dict):
            direct = _valid_bundle_ref(item.get("output_bundle"))
            if direct:
                return direct
            for nested in item.values():
                found = _walk(nested, depth + 1)
                if found:
                    return found
            return None
        for nested in item:
            found = _walk(nested, depth + 1)
            if found:
                return found
        return None

    return _walk(value)


def _safe_relative_path(value: Any, *, max_depth: int) -> Path:
    text = str(value or "").strip()
    if not text or "\\" in text or "\x00" in text:
        raise ArtifactMaterializationError("Artifact bundle contains an unsafe path")
    relative = PurePosixPath(text)
    parts = relative.parts
    if relative.is_absolute() or not parts or len(parts) > max_depth:
        raise ArtifactMaterializationError(
            f"Artifact bundle path is invalid or too deep: {text}"
        )
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactMaterializationError(
            f"Artifact bundle path traversal was rejected: {text}"
        )
    return Path(*parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _already_valid(path: Path, *, size: int, sha256: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return path.stat().st_size == size and _sha256_file(path) == sha256
    except OSError:
        return False


def _download_artifact(
    *,
    base_url: str,
    artifact_id: str,
    target: Path,
    expected_size: int,
    expected_sha256: str,
    max_file_bytes: int,
    preserve_existing: bool = False,
    headers: Optional[dict[str, str]] = None,
) -> bool:
    """Download one artifact atomically; return False when reused locally."""

    if _already_valid(target, size=expected_size, sha256=expected_sha256):
        return False
    if not target.parent.is_dir():
        raise ArtifactMaterializationError(
            f"Artifact destination parent is unavailable: {target.parent}"
        )
    if target.is_symlink():
        raise ArtifactMaterializationError(
            f"Artifact destination is a symlink: {target}"
        )
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    request = Request(
        f"{base_url}/api/v1/artifacts/{quote(artifact_id, safe='')}/content",
        headers=_artifact_headers() if headers is None else headers,
        method="GET",
    )
    token = request.headers.pop('X-artifact-token', None)
    if token:
        request.add_unredirected_header('X-Artifact-Token', token)
    digest = hashlib.sha256()
    received = 0
    try:
        with urlopen(request, timeout=_request_timeout()) as response, temp.open(
            "xb"
        ) as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size or received > max_file_bytes:
                    raise ArtifactMaterializationError(
                        f"Artifact {artifact_id} exceeded its declared size"
                    )
                digest.update(chunk)
                output.write(chunk)
        if received != expected_size:
            raise ArtifactMaterializationError(
                f"Artifact {artifact_id} size mismatch: "
                f"expected {expected_size}, got {received}"
            )
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise ArtifactMaterializationError(
                f"Artifact {artifact_id} sha256 mismatch: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        if preserve_existing:
            # Atomic publish without replacing a file changed during transfer.
            os.link(temp, target)
        else:
            os.replace(temp, target)
        return True
    except ArtifactMaterializationError:
        raise
    except (HTTPError, URLError, OSError) as exc:
        raise _response_error(exc) from exc
    finally:
        temp.unlink(missing_ok=True)


def _safe_target(root: Path, relative_path: Path) -> Path:
    """Create parent directories without following bundle-controlled symlinks."""

    current = root
    for part in relative_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ArtifactMaterializationError(
                f"Artifact bundle destination contains a symlink: {current}"
            )
        if current.exists():
            if not current.is_dir():
                raise ArtifactMaterializationError(
                    f"Artifact bundle destination is not a directory: {current}"
                )
        else:
            current.mkdir()
    target = current / relative_path.name
    if target.is_symlink():
        raise ArtifactMaterializationError(
            f"Artifact destination is a symlink: {target}"
        )
    try:
        target.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ArtifactMaterializationError(
            f"Artifact bundle destination escaped its root: {relative_path}"
        ) from exc
    return target


def materialize_output_bundle(
    value: Any,
    destination: str | Path,
    *,
    base_url: str = "",
    preserve_existing: bool = False,
    automatic: bool = False,
    selected_paths: Optional[list[str]] = None,
    expected_file: Optional[dict[str, Any]] = None,
    rename_conflicts: bool = True,
) -> Optional[dict[str, Any]]:
    """Download and verify an ``output_bundle`` below ``destination``.

    The returned paths are absolute. Callers can additionally map them to
    workspace-relative paths for Agent prompts and manifests.
    """

    bundle_ref = find_output_bundle(value)
    if bundle_ref is None:
        return None
    bundle_id = bundle_ref["bundle_id"]
    service_id = bundle_ref.get('service_id', '')
    selected, configured_url, headers = _artifact_endpoint(service_id)
    if selected and base_url and base_url.rstrip('/') != configured_url:
        raise ArtifactMaterializationError('Artifact service_id conflicts with base_url')
    service_url = str(base_url or configured_url).rstrip("/")
    metadata = _request_json(
        f"{service_url}/api/v1/bundles/{quote(bundle_id, safe='')}",
        **({'headers': headers} if selected else {}),
    )
    if str(metadata.get("bundle_id") or "").strip() != bundle_id:
        raise ArtifactMaterializationError("Artifact bundle identity mismatch")
    from .artifact_services import bind_artifact_source
    try:
        metadata = bind_artifact_source(metadata, selected)
    except ValueError as exc:
        raise ArtifactMaterializationError(str(exc)) from exc
    if str(metadata.get("state") or "").strip().lower() != "ready":
        raise ArtifactMaterializationError(
            f"Artifact bundle is not ready: {metadata.get('state')}"
        )
    entries = metadata.get("files")
    if not isinstance(entries, list):
        raise ArtifactMaterializationError("Artifact bundle file list is missing")

    max_files = _env_int(
        "TCODE_ARTIFACT_MATERIALIZE_MAX_FILES",
        _env_int("TMCP_ARTIFACT_MAX_BUNDLE_FILES", 10_000),
    )
    max_bundle_bytes = _env_int(
        "TCODE_ARTIFACT_MATERIALIZE_MAX_BYTES",
        _env_int("TMCP_ARTIFACT_MAX_BUNDLE_BYTES", 5 * 1024 * 1024 * 1024),
    )
    max_file_bytes = _env_int(
        "TCODE_ARTIFACT_MATERIALIZE_MAX_FILE_BYTES",
        _env_int("TMCP_ARTIFACT_MAX_FILE_BYTES", 500 * 1024 * 1024),
    )
    max_depth = _env_int(
        "TCODE_ARTIFACT_MATERIALIZE_MAX_TREE_DEPTH",
        _env_int("TMCP_ARTIFACT_MAX_TREE_DEPTH", 32),
    )
    if len(entries) > max_files:
        raise ArtifactMaterializationError(
            f"Artifact bundle has too many files: {len(entries)} > {max_files}"
        )

    normalized: list[dict[str, Any]] = []
    relative_paths: set[str] = set()
    total_size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ArtifactMaterializationError(
                "Artifact bundle contains an invalid file entry"
            )
        artifact_id = str(entry.get("artifact_id") or "").strip()
        if not _PORTABLE_ID.fullmatch(artifact_id):
            raise ArtifactMaterializationError(
                f"Artifact bundle contains an invalid artifact id: {artifact_id}"
            )
        try:
            size = int(entry.get("size"))
        except (TypeError, ValueError) as exc:
            raise ArtifactMaterializationError(
                f"Artifact {artifact_id} has an invalid size"
            ) from exc
        sha256 = str(entry.get("sha256") or "").strip().lower()
        if size < 0:
            raise ArtifactMaterializationError(
                f"Artifact {artifact_id} exceeds the materialization file limit"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ArtifactMaterializationError(
                f"Artifact {artifact_id} has an invalid sha256"
            )
        relative_path = _safe_relative_path(
            entry.get("relative_path"), max_depth=max_depth
        )
        relative_key = relative_path.as_posix()
        if relative_key in relative_paths:
            raise ArtifactMaterializationError(
                f"Artifact bundle contains a duplicate path: {relative_key}"
            )
        relative_paths.add(relative_key)
        total_size += size
        normalized.append(
            {
                "artifact_id": artifact_id,
                "relative_path": relative_path,
                "size": size,
                "sha256": sha256,
            }
        )

    try:
        declared_count = metadata.get("total_files")
        if declared_count is not None and int(declared_count) != len(normalized):
            raise ArtifactMaterializationError("Artifact bundle file count mismatch")
        declared_size = metadata.get("total_size")
        if declared_size is not None and int(declared_size) != total_size:
            raise ArtifactMaterializationError("Artifact bundle total size mismatch")
    except (TypeError, ValueError) as exc:
        raise ArtifactMaterializationError(
            "Artifact bundle totals are invalid"
        ) from exc

    # Large automatic transfers disclose metadata only. Do not guess which
    # scientific files matter from their names; the existing query tool selects.
    catalog = {"bundle_id": bundle_id, "total_files": len(normalized), "total_size": total_size,
               "files": [{**item, "relative_path": item["relative_path"].as_posix()} for item in normalized]}
    if selected:
        catalog['service_id'] = selected
    deferred = automatic and (
        total_size > min(max_bundle_bytes, _env_int("TCODE_ARTIFACT_AUTO_MAX_BYTES", 16 * 1024 * 1024))
        or any(item["size"] > min(max_file_bytes, _env_int("TCODE_ARTIFACT_AUTO_MAX_FILE_BYTES", 4 * 1024 * 1024)) for item in normalized)
    )
    if selected_paths is not None:
        requested_paths = set(selected_paths)
        if not requested_paths <= {item["relative_path"].as_posix() for item in normalized}:
            raise ArtifactMaterializationError("Selected artifact is absent from the bundle")
        normalized = [item for item in normalized if item["relative_path"].as_posix() in requested_paths]
        if expected_file is not None and (len(normalized) != 1 or any(
            catalog_item != expected_file for catalog_item in catalog["files"]
            if catalog_item["relative_path"] in requested_paths
        )):
            raise ArtifactMaterializationError("Selected artifact metadata changed")
        deferred = False
    elif deferred:
        normalized = []
    selected_size = sum(item["size"] for item in normalized)
    if selected_size > max_bundle_bytes or any(item["size"] > max_file_bytes for item in normalized):
        raise ArtifactMaterializationError("Selected artifacts exceed the download size limit; remote files remain available")

    destination_root = Path(destination).expanduser().resolve(strict=False)
    destination_root.mkdir(parents=True, exist_ok=True)
    root_candidate = destination_root / (f'{selected}--{bundle_id}' if selected else bundle_id)
    if root_candidate.is_symlink():
        raise ArtifactMaterializationError(
            f"Artifact bundle destination is a symlink: {root_candidate}"
        )
    root_candidate.mkdir(parents=True, exist_ok=True)
    root = root_candidate.resolve(strict=True)
    try:
        root.relative_to(destination_root.resolve(strict=True))
    except ValueError as exc:
        raise ArtifactMaterializationError(
            "Artifact bundle destination escaped the materialization root"
        ) from exc
    materialized_files: list[dict[str, Any]] = []
    downloaded_count = 0
    for entry in normalized:
        target = _safe_target(root, entry["relative_path"])
        if preserve_existing:
            original = target
            collision = 0
            while target.exists() and not _already_valid(target, size=entry["size"], sha256=entry["sha256"]):
                if not rename_conflicts:
                    raise ArtifactMaterializationError("artifact_target_conflict")
                collision += 1
                target = _safe_target(root, original.relative_to(root).with_name(f"{original.stem}.received-{entry['sha256'][:12]}-{collision}{original.suffix}"))
        downloaded = _download_artifact(
            base_url=service_url,
            artifact_id=entry["artifact_id"],
            target=target,
            expected_size=entry["size"],
            expected_sha256=entry["sha256"],
            max_file_bytes=max_file_bytes,
            preserve_existing=preserve_existing,
            **({'headers': headers} if selected else {}),
        )
        downloaded_count += int(downloaded)
        materialized_files.append(
            {
                "path": target.as_posix(),
                "relative_path": target.relative_to(root).as_posix(),
                "artifact_id": entry["artifact_id"],
                "size": entry["size"],
                "sha256": entry["sha256"],
                "downloaded": downloaded,
            }
        )

    return {
        "status": "succeeded",
        "bundle_id": bundle_id,
        "service_url": service_url,
        "service_id": selected,
        "root": root.as_posix(),
        "total_files": len(materialized_files),
        "total_size": selected_size,
        "deferred": deferred,
        "remote_catalog": catalog if deferred else None,
        "downloaded_count": downloaded_count,
        "reused_count": len(materialized_files) - downloaded_count,
        "files": materialized_files,
    }
