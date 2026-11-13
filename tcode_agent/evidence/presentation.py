"""Presentation helpers for evidence records and visual artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
PATH_KEYS = {
    "figure_image_paths",
    "image_path",
    "markdown_path",
    "page_image_path",
    "page_image_paths",
    "pdf_path",
    "record_path",
    "source_path",
    "table_image_path",
    "tables_extract_path",
    "visual_manifest_path",
    "workdir",
    "workspace_root",
    "library_root",
}
IMAGE_FIELDS = (
    ("page_image_path", "page"),
    ("page_image_paths", "page"),
    ("table_image_path", "table"),
    ("figure_image_paths", "figure"),
    ("image_path", "image"),
)
IMAGE_KIND_LABELS = {
    "page": "论文页（Page）",
    "table": "表格（Table）",
    "figure": "论文图（Figure）",
    "image": "图片（Image）",
}


def _payload(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return dict(record)
    to_dict = getattr(record, "to_dict", None)
    return dict(to_dict() if callable(to_dict) else {})


def _path_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _path_values(item)


def _image_sources(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    artifact_refs = payload.get("artifact_refs")
    if isinstance(artifact_refs, dict):
        yield artifact_refs
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("paper_anchor", "paper_chunk", "paper_visual"):
            item = metadata.get(key)
            if isinstance(item, dict):
                yield item
    for citation in payload.get("citations") or []:
        if isinstance(citation, dict):
            yield citation


def evidence_image_entries(record: Any) -> list[dict[str, Any]]:
    payload = _payload(record)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in _image_sources(payload):
        for field, kind in IMAGE_FIELDS:
            for raw_path in _path_values(source.get(field)):
                if Path(raw_path).suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                identity = str(Path(raw_path).expanduser())
                if identity in seen:
                    continue
                seen.add(identity)
                name = Path(raw_path).name or "image"
                entries.append(
                    {
                        "index": len(entries),
                        "kind": kind,
                        "label": f"{IMAGE_KIND_LABELS.get(kind, IMAGE_KIND_LABELS['image'])} · {name}",
                        "name": name,
                        "path": raw_path,
                    }
                )
    return entries


def evidence_image_manifest(record: Any) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in entry.items() if key != "path"}
        for entry in evidence_image_entries(record)
    ]


def resolve_evidence_image(record: Any, image_index: int, *, workdir: str) -> Path:
    entries = evidence_image_entries(record)
    try:
        if int(image_index) < 0:
            raise IndexError("Evidence image not found")
        entry = entries[int(image_index)]
    except (IndexError, TypeError, ValueError) as exc:
        raise IndexError("Evidence image not found") from exc
    root = Path(workdir).expanduser().resolve()
    raw_path = Path(str(entry.get("path") or "")).expanduser()
    target = (root / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Evidence image is outside the workspace") from exc
    if target.suffix.lower() not in IMAGE_SUFFIXES or not target.is_file():
        raise FileNotFoundError("Evidence image not found")
    return target


def _short_path(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        return text.replace("\\", "/").rsplit("/", 1)[-1] if text else ""
    if isinstance(value, list):
        return [_short_path(item) for item in value]
    return value


def abbreviate_storage_paths(value: Any, *, key: str = "") -> Any:
    normalized_key = key.lower()
    if (
        normalized_key in PATH_KEYS
        or normalized_key.endswith("_path")
        or normalized_key.endswith("_paths")
    ):
        return _short_path(value)
    if isinstance(value, dict):
        return {
            str(item_key): abbreviate_storage_paths(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [abbreviate_storage_paths(item) for item in value]
    return value


def present_evidence_record(record: Any) -> dict[str, Any]:
    payload = _payload(record)
    payload.pop("embedding", None)
    payload.pop("search_text", None)
    presented = abbreviate_storage_paths(payload)
    presented["images"] = evidence_image_manifest(payload)
    return presented


def present_evidence_graph(graph: dict[str, Any]) -> dict[str, Any]:
    presented = dict(graph or {})
    nodes: list[dict[str, Any]] = []
    for raw_node in presented.get("nodes") or []:
        node = dict(raw_node or {})
        node_type = str(node.get("type") or node.get("node_type") or "").lower()
        if node_type in {"artifact", "paper_visual"}:
            node["label"] = _short_path(str(node.get("label") or ""))
        nodes.append(node)
    presented["nodes"] = nodes
    return presented
