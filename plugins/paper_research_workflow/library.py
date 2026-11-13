"""Layered local paper library storage and retrieval helpers."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from tcode_agent.memory.retriever import build_embedding, semantic_rank
from tcode_agent.memory.vector_store import FaissVectorStore

try:
    from .text_safety import find_valid_mutations, strip_pdf_control_codes
except ImportError:
    # 隔离 Worker 会把 library.py 作为独立模块加载，因此需要显式加载同目录辅助模块。
    _TEXT_SAFETY_PATH = Path(__file__).resolve().parent / "text_safety.py"
    _TEXT_SAFETY_SPEC = importlib.util.spec_from_file_location(
        "paper_research_workflow_text_safety",
        _TEXT_SAFETY_PATH,
    )
    if _TEXT_SAFETY_SPEC is None or _TEXT_SAFETY_SPEC.loader is None:
        raise ImportError(f"cannot load paper text safety module: {_TEXT_SAFETY_PATH}")
    _TEXT_SAFETY_MODULE = importlib.util.module_from_spec(_TEXT_SAFETY_SPEC)
    _TEXT_SAFETY_SPEC.loader.exec_module(_TEXT_SAFETY_MODULE)
    find_valid_mutations = _TEXT_SAFETY_MODULE.find_valid_mutations
    strip_pdf_control_codes = _TEXT_SAFETY_MODULE.strip_pdf_control_codes


TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
PMID_RE = re.compile(r"\bPMID\s*[:#]?\s*(\d{6,9})\b", re.IGNORECASE)
GENE_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9-]{2,20}\b")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;。！？])\s+")
SENTENCE_ABBREVIATION_RE = re.compile(
    r"\b(?:fig|figs|eq|eqs|ref|refs|dr|mr|mrs|ms|vs|al)\.",
    re.IGNORECASE,
)
KINETIC_RE = re.compile(
    r"\b(?:kcat\s*/\s*km|kcat|km|kcat/km|specific activity|activity|kinetic)\b",
    re.IGNORECASE,
)
SUBSTRATE_RE = re.compile(r"\bsubstrate[s]?\b", re.IGNORECASE)
ACTIVITY_RE = re.compile(
    r"\b(?:activity|conversion|yield|enantioselectivity|specific activity)\b",
    re.IGNORECASE,
)
EXPERIMENT_RE = re.compile(
    r"\b(?:condition|conditions|buffer|temperature|temp|ph|cofactor|incubation|assay)\b",
    re.IGNORECASE,
)
FIGURE_CAPTION_RE = re.compile(
    r"\b(?:fig(?:ure)?\.?\s*\d+[a-z]?)[\s:.-]+.{0,360}",
    re.IGNORECASE,
)
HEADER_METRIC_RE = re.compile(
    r"\b(?:km|kcat|activity|yield|conversion|selectivity|enantioselectivity)\b",
    re.IGNORECASE,
)
SECTION_HINTS = (
    ("abstract", re.compile(r"\babstract\b", re.IGNORECASE)),
    ("introduction", re.compile(r"\bintroduction\b", re.IGNORECASE)),
    ("methods", re.compile(r"\b(?:methods?|materials?)\b", re.IGNORECASE)),
    ("results", re.compile(r"\bresults?\b", re.IGNORECASE)),
    ("discussion", re.compile(r"\bdiscussion\b", re.IGNORECASE)),
    ("conclusion", re.compile(r"\bconclusions?\b", re.IGNORECASE)),
)
QUESTION_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gene": ("gene", "genes", "基因", "酶"),
    "mutation": ("mutation", "mutant", "variant", "突变", "位点"),
    "substrate": ("substrate", "substrates", "底物", "底物范围"),
    "kinetics": ("km", "kcat", "kinetic", "动力学", "活性"),
    "table": ("table", "表", "表格"),
    "figure": ("figure", "fig", "图"),
    "condition": ("condition", "pH", "temperature", "buffer", "条件"),
}
STOPWORDS = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "were",
    "have",
    "shown",
    "using",
    "into",
    "structure",
    "engineering",
    "guided",
    "meso",
    "diamino",
    "dehydrogenase",
    "reductive",
    "amination",
    "sterically",
    "bulky",
    "enzyme",
    "variant",
    "mutant",
    "paper",
    "study",
    "results",
}
VISUAL_ARTIFACT_SCHEMA_VERSION = 2
DOC_CHUNK_INDEX_SCHEMA_VERSION = 1
EVIDENCE_BRAIN_SCHEMA_VERSION = 3


_INGEST_PROCESS_LOCKS_GUARD = threading.Lock()
_INGEST_PROCESS_LOCKS: dict[str, threading.RLock] = {}


@dataclass
class PaperLibraryConfig:
    chunk_max_chars: int = 900
    chunk_overlap_chars: int = 120
    allow_cross_page_chunks: bool = True
    paragraph_summary_max_chars: int = 220
    query_candidate_doc_limit: int = 5
    query_candidate_passage_limit: int = 8
    query_evidence_limit: int = 8
    # 单篇论文最多同步多少个 L2 Context Chunk 到 EvidenceStore。表格 Chunk
    # 会优先进入该有界集合，避免后置表格被简单的前 N 条截断遗漏。
    evidence_context_max_records: int = 500
    # Proposal 生成时，单次最多按 Claim 的真实来源引用补齐多少条 Evidence。
    # 该限制独立于上面的常规预同步上限，不影响普通检索和 Wiki 页面读取。
    evidence_ensure_max_references: int = 200
    # 相对相关性阈值 (0.0=禁用，仅用 limit；>0 时取所有 score/max_score >= 阈值的文档，上限为 query_candidate_doc_max_hits)
    query_candidate_doc_score_threshold: float = 0.0
    # 阈值模式下的命中数上限，防止上下文爆炸
    query_candidate_doc_max_hits: int = 20
    render_page_images: bool = True
    render_table_images: bool = True
    render_figure_images: bool = True
    visual_image_dpi: int = 200
    visual_image_max_dim: int = 1000
    visual_table_padding_px: int = 12
    visual_figure_min_dim: int = 48
    visual_figure_min_area: int = 4096
    visual_max_pages: int = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(text or "")).strip("._")
    return slug or fallback


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(str(text or ""))]


def _normalize_whitespace(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"-\s*\n(?=\w)", "", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    return normalized.strip()


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_path_prefix(payload: Any, old_root: Path, new_root: Path) -> Any:
    """Rewrite one normalized path root in a nested payload.

    Only a complete path prefix is eligible.  A source-root spelling that
    merely appears inside another absolute path must remain untouched.  The
    explicit new-root check also makes repeated publication passes idempotent,
    including when the destination itself is nested below the source root.
    """

    old_text = old_root.resolve(strict=False).as_posix().rstrip("/")
    new_text = new_root.resolve(strict=False).as_posix().rstrip("/")
    if not old_text or old_text == new_text:
        return payload

    # Match path tokens at the beginning of a string or after a non-path
    # character.  The destination alternative is checked first and preserved,
    # which makes a second rewrite pass a no-op even when new_root itself lives
    # below old_root.  A source spelling in the middle of another path (for
    # example ``/srv/my-workspace/root``) therefore cannot be rewritten.
    boundary = r"(?<![\w.~+\-\\/])"
    suffix = r"(?=$|/)"
    pattern = re.compile(
        rf"{boundary}(?:(?P<new>{re.escape(new_text)})|"
        rf"(?P<old>{re.escape(old_text)})){suffix}"
    )

    def rewrite(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str):
            return pattern.sub(
                lambda match: match.group("new") or new_text,
                value,
            )
        return value

    return rewrite(payload)


def _normalize_row(row: list[Any]) -> list[str]:
    return [("" if cell is None else str(cell).strip()) for cell in row]


def _table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max((len(row) for row in rows), default=0)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    separator = ["---"] * width
    body = normalized[1:] if len(normalized) > 1 else []
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _top_keywords(text: str, limit: int = 8) -> list[str]:
    counts: Counter[str] = Counter()
    for token in _tokenize(text):
        if len(token) < 3 or token in STOPWORDS:
            continue
        counts[token] += 1
    return [item for item, _ in counts.most_common(limit)]


def _first_sentence(text: str, max_chars: int) -> str:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return ""
    parts = [part.strip() for part in SENTENCE_SPLIT_RE.split(raw) if part.strip()]
    candidate = parts[0] if parts else raw
    if len(candidate) <= max_chars:
        return candidate
    return candidate[: max_chars - 3].rstrip() + "..."


def _sentence_units(text: str) -> list[str]:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return []
    protected = SENTENCE_ABBREVIATION_RE.sub(
        lambda match: match.group(0).replace(".", "__TCODE_DOT__"),
        raw,
    )
    return [
        part.replace("__TCODE_DOT__", ".").strip()
        for part in SENTENCE_SPLIT_RE.split(protected)
        if part.strip()
    ]


def _question_types_from_text(text: str) -> list[str]:
    lower = str(text or "").lower()
    matches: list[str] = []
    for label, keywords in QUESTION_TYPE_KEYWORDS.items():
        if any(keyword.lower() in lower for keyword in keywords):
            matches.append(label)
    return matches


def _json_compact(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(payload)


class PaperLibraryStore:
    def __init__(
        self,
        *,
        library_root: Path,
        workdir: str,
        config: Optional[PaperLibraryConfig] = None,
        path_rewrites: Optional[list[tuple[str, str]]] = None,
    ):
        self.library_root = library_root.resolve()
        self.workdir = workdir
        self.config = config or PaperLibraryConfig()
        self.path_rewrites: list[tuple[str, str]] = []
        for source, target in list(path_rewrites or []):
            source_text = str(source or "").strip()
            target_text = str(target or "").strip()
            if not source_text or not target_text:
                continue
            physical_root = Path(source_text).expanduser().resolve(strict=False).as_posix().rstrip("/")
            published_root = Path(target_text).expanduser().as_posix().rstrip("/")
            if physical_root != published_root:
                self.path_rewrites.append((physical_root, published_root))
        self._evidence_sync_locks_guard = threading.Lock()
        self._evidence_sync_locks: dict[str, threading.RLock] = {}

    def _evidence_sync_lock(self, doc_id: str) -> threading.RLock:
        key = str(doc_id or "").strip()
        with self._evidence_sync_locks_guard:
            lock = self._evidence_sync_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._evidence_sync_locks[key] = lock
            return lock

    @property
    def docs_root(self) -> Path:
        return self.library_root / "docs"

    @property
    def registry_path(self) -> Path:
        return self.library_root / "registry.json"

    @property
    def chunk_index_path(self) -> Path:
        return self.library_root / "index" / "chunks.json"

    @property
    def chunk_index_base(self) -> Path:
        return self.library_root / "index" / "chunks"

    @property
    def staging_root(self) -> Path:
        return self.library_root / ".staging"

    @property
    def locks_root(self) -> Path:
        return self.library_root / ".locks"

    def _doc_chunk_index_base(self, doc_id: str, *, doc_dir: Optional[Path] = None) -> Path:
        return (doc_dir or self._doc_dir(doc_id)) / "index" / "chunks"

    def _faiss_index_path(self, base_path: Path) -> Path:
        return base_path.with_suffix(base_path.suffix + ".faiss")

    def _faiss_meta_path(self, base_path: Path) -> Path:
        return base_path.with_suffix(base_path.suffix + ".faiss.meta.json")

    def _ensure_dirs(self) -> None:
        self.docs_root.mkdir(parents=True, exist_ok=True)
        self.chunk_index_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_registry(self) -> dict[str, Any]:
        self._ensure_dirs()
        payload = _read_json(self.registry_path, {})
        if not isinstance(payload, dict):
            payload = {}
        docs = payload.get("documents")
        if not isinstance(docs, list):
            payload["documents"] = []
        return payload

    def _save_registry(self, payload: dict[str, Any]) -> None:
        normalized = dict(payload or {})
        normalized["updated_at"] = _utc_now()
        docs = normalized.get("documents")
        normalized["documents"] = docs if isinstance(docs, list) else []
        _write_json(self.registry_path, self._without_embeddings(normalized))

    def _compute_sha256(self, pdf_path: Path) -> str:
        digest = hashlib.sha256()
        with pdf_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _doc_id(self, sha256: str) -> str:
        return f"paper-{sha256[:16]}"

    def _doc_dir(self, doc_id: str) -> Path:
        return self.docs_root / _safe_slug(doc_id, "paper")

    def _doc_version_dir(self, doc_id: str, version_id: str) -> Path:
        return self._doc_dir(doc_id) / "versions" / _safe_slug(version_id, "version")

    @contextmanager
    def _file_lock(self, name: str):
        """Serialize ingest/registry commits across threads and worker processes."""

        lock_path = self.locks_root / f"{_safe_slug(name, 'library')}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        key = lock_path.resolve(strict=False).as_posix()
        with _INGEST_PROCESS_LOCKS_GUARD:
            process_lock = _INGEST_PROCESS_LOCKS.setdefault(key, threading.RLock())
        with process_lock:
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    pass
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    pass
                handle.close()

    def _record_path_for_entry(self, entry: dict[str, Any]) -> Optional[Path]:
        raw = str(
            (entry.get("artifacts") or {}).get("record_path")
            or entry.get("record_path")
            or ""
        ).strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.library_root / path
        if not path.exists():
            raw_path = path.as_posix()
            for physical_root, published_root in self.path_rewrites:
                if raw_path == published_root or raw_path.startswith(f"{published_root}/"):
                    path = Path(f"{physical_root}{raw_path[len(published_root):]}")
                    break
        return path.resolve(strict=False)

    def _published_path(self, path: Path) -> Path:
        value = path.resolve(strict=False).as_posix()
        for physical_root, published_root in self.path_rewrites:
            if value == physical_root or value.startswith(f"{physical_root}/"):
                return Path(f"{published_root}{value[len(physical_root):]}")
        return Path(value)

    def _publish_payload_paths(self, payload: Any) -> Any:
        rewritten = payload
        for physical_root, published_root in self.path_rewrites:
            rewritten = _replace_path_prefix(
                rewritten,
                Path(physical_root),
                Path(published_root),
            )
        return rewritten

    def _record_path_for_doc(self, doc_id: str) -> Path:
        entry = self._find_registry_entry(doc_id=str(doc_id or "").strip())
        if entry:
            record_path = self._record_path_for_entry(entry)
            if record_path is not None:
                return record_path
        return self._doc_dir(doc_id) / "record.json"

    def _write_record(self, doc_id: str, record: dict[str, Any]) -> None:
        _write_json(
            self._record_path_for_doc(doc_id),
            self._without_embeddings(record),
        )

    def import_reference_record(
        self,
        record: dict[str, Any],
        *,
        pack_id: str,
        source_doc_id: str,
        record_hash: str = "",
    ) -> dict[str, Any]:
        """Publish an imported reference-only paper as a searchable library record.

        Reference imports deliberately expose only metadata, L0 routing text, L1
        passages, and anchors.  Missing PDF/L2 layers are declared rather than
        represented by empty chunks, so callers cannot mistake structural
        placeholders for evidence.
        """

        payload = dict(record or {})
        doc_id = str(payload.get("doc_id") or "").strip()
        if not doc_id:
            raise ValueError("reference paper doc_id is required")
        metadata = dict(payload.get("metadata") or {})
        title = str(metadata.get("title") or payload.get("title") or source_doc_id or doc_id).strip()
        passages = [
            dict(item)
            for item in list(payload.get("l1_passage_summaries") or [])
            if isinstance(item, dict)
        ]
        anchors = [
            dict(item)
            for item in list(payload.get("evidence_anchors") or [])
            if isinstance(item, dict)
        ]
        keywords = list(
            dict.fromkeys(
                str(item or "").strip()
                for passage in passages
                for item in list(passage.get("keywords") or [])
                if str(item or "").strip()
            )
        )[:80]
        question_types = list(
            dict.fromkeys(
                str(item or "").strip()
                for passage in passages
                for item in list(passage.get("question_types") or [])
                if str(item or "").strip()
            )
        )[:40]
        page_values = [
            int(value)
            for passage in passages
            for value in list(passage.get("page_span") or passage.get("source_pages") or [])
            if str(value).isdigit()
        ]
        summary_text = next(
            (
                str(item.get("summary_text") or "").strip()
                for item in passages
                if str(item.get("summary_text") or "").strip()
            ),
            title,
        )[:1200]
        l0_summary = {
            "doc_id": doc_id,
            "summary_text": summary_text,
            "keywords": keywords,
            "question_types": question_types,
            "page_span": [min(page_values), max(page_values)] if page_values else [],
            "source": "researchpack_reference",
        }
        available_layers = ["metadata", "l0_summary"]
        if passages:
            available_layers.append("passages")
        if anchors:
            available_layers.append("anchors")
        missing_layers = ["pdf", "facts", "chunks", "tables", "doc_chunk_index"]
        doc_dir = self._doc_dir(doc_id)
        record_path = doc_dir / "record.json"
        now = _utc_now()
        normalized = {
            **payload,
            "doc_id": doc_id,
            "metadata": {
                **metadata,
                "title": title,
                "imported_from_pack_id": str(pack_id or ""),
                "source_doc_id": str(source_doc_id or ""),
                "distribution": "reference_only",
            },
            "pdf_path": "",
            "raw_artifacts": {},
            "l0_summary": l0_summary,
            "l1_passage_summaries": passages,
            "l2_facts": [],
            "l2_chunks": [],
            "materialized_views": {},
            "evidence_anchors": anchors,
            "visual_artifacts": {},
            "doc_chunk_index": {},
            "artifacts": {"record_path": record_path.resolve(strict=False).as_posix()},
            "available_layers": available_layers,
            "missing_layers": missing_layers,
            "degraded_reason": "reference_only_import",
            "imported_reference_only": True,
            "updated_at": now,
        }
        digest = str(record_hash or "").strip() or hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        search_text = " ".join(
            [
                title,
                summary_text,
                " ".join(keywords),
                " ".join(
                    str(item.get("summary_text") or "")
                    for item in passages[:100]
                ),
            ]
        )[:100000]
        identifiers = dict(metadata.get("identifiers") or {})
        for key in ("pmid", "doi"):
            value = str(metadata.get(key) or payload.get(key) or "").strip()
            if value:
                identifiers[key] = value
        registry_entry = {
            "doc_id": doc_id,
            "sha256": digest,
            "status": "ready",
            "title": title,
            "pdf_path": "",
            "all_paths": [],
            "identifiers": identifiers,
            "l0_summary_index": l0_summary,
            "fact_counts": {},
            "visual_counts": {},
            "search_text": search_text,
            "record_path": record_path.resolve(strict=False).as_posix(),
            "artifacts": {"record_path": record_path.resolve(strict=False).as_posix()},
            "available_layers": available_layers,
            "missing_layers": missing_layers,
            "degraded_reason": "reference_only_import",
            "distribution": "reference_only",
            "imported_reference_only": True,
            "imported_from_pack_id": str(pack_id or ""),
            "source_doc_id": str(source_doc_id or ""),
            "updated_at": now,
        }

        doc_dir.mkdir(parents=True, exist_ok=False)
        try:
            _write_json(record_path, self._without_embeddings(normalized))
            with self._file_lock("registry"):
                registry = self._load_registry()
                documents = [
                    dict(item)
                    for item in registry.get("documents", [])
                    if isinstance(item, dict)
                ]
                if any(str(item.get("doc_id") or "") == doc_id for item in documents):
                    raise FileExistsError(f"paper library record already exists: {doc_id}")
                registry["documents"] = sorted(
                    [*documents, registry_entry],
                    key=lambda item: str(item.get("updated_at") or ""),
                    reverse=True,
                )
                self._save_registry(registry)
        except BaseException:
            shutil.rmtree(doc_dir, ignore_errors=True)
            raise
        return {"record": normalized, "registry_entry": registry_entry}

    def import_portable_record(
        self,
        record: dict[str, Any],
        *,
        pack_id: str,
        source_doc_id: str,
        record_hash: str = "",
    ) -> dict[str, Any]:
        """Publish a no-PDF ResearchPack record with all declared derived layers."""

        payload = self._without_embeddings(dict(record or {}))
        doc_id = str(payload.get("doc_id") or "").strip()
        if not doc_id:
            raise ValueError("portable paper doc_id is required")
        metadata = dict(payload.get("metadata") or {})
        title = str(metadata.get("title") or payload.get("title") or source_doc_id or doc_id).strip()
        l0_summary = dict(payload.get("l0_summary") or {})
        passages = [dict(item) for item in list(payload.get("l1_passage_summaries") or []) if isinstance(item, dict)]
        facts = [dict(item) for item in list(payload.get("l2_facts") or []) if isinstance(item, dict)]
        chunks = [dict(item) for item in list(payload.get("l2_chunks") or []) if isinstance(item, dict)]
        tables = [dict(item) for item in list(payload.get("structured_tables") or []) if isinstance(item, dict)]
        anchors = [dict(item) for item in list(payload.get("evidence_anchors") or []) if isinstance(item, dict)]
        views = dict(payload.get("materialized_views") or {})
        figures = [dict(item) for item in list(payload.get("figure_descriptions") or []) if isinstance(item, dict)]
        logical_index = dict(payload.get("doc_chunk_index") or {})
        for collection in (passages, facts, chunks, tables, anchors, figures):
            for item in collection:
                item["doc_id"] = doc_id
        for item in list(logical_index.get("entries") or []):
            if isinstance(item, dict):
                item["doc_id"] = doc_id
        available_layers = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in list(payload.get("available_layers") or [])
                if str(item or "").strip() and str(item or "").strip() != "original_pdf"
            )
        )
        missing_layers = list(
            dict.fromkeys(
                [
                    "original_pdf",
                    *[
                        str(item or "").strip()
                        for item in list(payload.get("missing_layers") or [])
                        if str(item or "").strip()
                    ],
                ]
            )
        )
        distribution = str(payload.get("distribution") or "derived_partial").strip()
        if distribution not in {"derived_complete", "derived_partial"}:
            distribution = "derived_partial"
        try:
            visual_schema_version = int((payload.get("parser") or {}).get("visual_schema_version") or 0)
        except (TypeError, ValueError):
            visual_schema_version = 0
        doc_dir = self._doc_dir(doc_id)
        record_path = doc_dir / "record.json"
        now = _utc_now()
        normalized = {
            **payload,
            "doc_id": doc_id,
            "metadata": {
                **metadata,
                "title": title,
                "imported_from_pack_id": str(pack_id or ""),
                "source_doc_id": str(source_doc_id or ""),
                "distribution": distribution,
            },
            "pdf_path": "",
            "raw_artifacts": {},
            "l0_summary": l0_summary,
            "l1_passage_summaries": passages,
            "l2_facts": facts,
            "l2_chunks": chunks,
            "structured_tables": tables,
            "materialized_views": views,
            "evidence_anchors": anchors,
            "figure_descriptions": figures,
            "visual_artifacts": {
                "schema_version": visual_schema_version,
                "figure_images": figures,
                "figure_image_count": len(figures),
                "binary_artifacts_embedded": False,
            },
            "doc_chunk_index": {
                **logical_index,
                "doc_chunk_index_available": False,
                "vector_index_embedded": False,
            },
            "artifacts": {"record_path": record_path.resolve(strict=False).as_posix()},
            "available_layers": available_layers,
            "missing_layers": missing_layers,
            "source_pdf_missing": True,
            "degraded_reason": "" if distribution == "derived_complete" else "derived_layers_partial",
            "imported_portable": True,
            "imported_reference_only": False,
            "imported_from_pack_id": str(pack_id or ""),
            "source_doc_id": str(source_doc_id or ""),
            "distribution": distribution,
            "updated_at": now,
        }
        digest = str(record_hash or "").strip() or hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        keywords = [str(item or "") for item in list(l0_summary.get("keywords") or [])]
        question_types = [str(item or "") for item in list(l0_summary.get("question_types") or [])]
        search_text = "\n".join(
            value
            for value in [
                title,
                str(l0_summary.get("summary_text") or ""),
                " ".join(keywords),
                *[str(item.get("summary_text") or "") for item in passages],
                *[str(item.get("search_text") or item.get("value") or "") for item in facts],
                *[str(item.get("search_text") or item.get("text") or "") for item in chunks],
            ]
            if value
        )[:500000]
        identifiers = dict(metadata.get("identifiers") or {})
        for key in ("pmid", "doi"):
            value = str(metadata.get(key) or payload.get(key) or "").strip()
            if value:
                identifiers[key] = value
        fact_counts = self._fact_counts(materialized_views=views, l2_facts=facts)
        registry_entry = {
            "doc_id": doc_id,
            "sha256": digest,
            "status": "ready",
            "title": title,
            "pdf_path": "",
            "all_paths": [],
            "identifiers": identifiers,
            "l0_summary_index": {
                "summary_text": str(l0_summary.get("summary_text") or ""),
                "keywords": keywords,
                "question_types": question_types,
                "page_span": list(l0_summary.get("page_span") or []),
            },
            "fact_counts": fact_counts,
            "visual_counts": {"figures": len(figures), "binary_artifacts": 0},
            "search_text": search_text,
            "record_path": record_path.resolve(strict=False).as_posix(),
            "artifacts": {"record_path": record_path.resolve(strict=False).as_posix()},
            "chunk_count": len(chunks),
            "passage_count": len(passages),
            "available_layers": available_layers,
            "missing_layers": missing_layers,
            "degraded_reason": normalized["degraded_reason"],
            "distribution": distribution,
            "source_pdf_missing": True,
            "imported_portable": True,
            "imported_reference_only": False,
            "imported_from_pack_id": str(pack_id or ""),
            "source_doc_id": str(source_doc_id or ""),
            "updated_at": now,
        }

        doc_dir.mkdir(parents=True, exist_ok=False)
        try:
            _write_json(record_path, normalized)
            with self._file_lock("registry"):
                registry = self._load_registry()
                documents = [dict(item) for item in registry.get("documents", []) if isinstance(item, dict)]
                if any(str(item.get("doc_id") or "") == doc_id for item in documents):
                    raise FileExistsError(f"paper library record already exists: {doc_id}")
                registry["documents"] = sorted(
                    [*documents, registry_entry],
                    key=lambda item: str(item.get("updated_at") or ""),
                    reverse=True,
                )
                self._save_registry(registry)
        except BaseException:
            shutil.rmtree(doc_dir, ignore_errors=True)
            raise
        return {"record": normalized, "registry_entry": registry_entry}

    def remove_imported_record(self, doc_id: str, *, pack_id: str = "") -> bool:
        """Remove one imported portable/reference record during rollback."""

        normalized_id = str(doc_id or "").strip()
        if not normalized_id:
            return False
        removed_entry = False
        matching_entry_remains = False
        with self._file_lock("registry"):
            registry = self._load_registry()
            documents = [
                dict(item)
                for item in registry.get("documents", [])
                if isinstance(item, dict)
            ]
            kept: list[dict[str, Any]] = []
            for item in documents:
                if str(item.get("doc_id") or "") != normalized_id:
                    kept.append(item)
                    continue
                if not bool(item.get("imported_reference_only") or item.get("imported_portable")):
                    kept.append(item)
                    matching_entry_remains = True
                    continue
                if pack_id and str(item.get("imported_from_pack_id") or "") != str(pack_id):
                    kept.append(item)
                    matching_entry_remains = True
                    continue
                removed_entry = True
            if removed_entry:
                registry["documents"] = kept
                self._save_registry(registry)
        record = self._load_record(normalized_id) or {}
        record_pack_id = str(
            (record.get("metadata") or {}).get("imported_from_pack_id")
            or record.get("imported_from_pack_id")
            or ""
        )
        orphan_matches = (
            not matching_entry_remains
            and bool(record.get("imported_reference_only") or record.get("imported_portable"))
            and (not pack_id or record_pack_id == str(pack_id))
        )
        if removed_entry or orphan_matches:
            shutil.rmtree(self._doc_dir(normalized_id), ignore_errors=True)
            return True
        return False

    def remove_imported_reference_record(self, doc_id: str, *, pack_id: str = "") -> bool:
        """Backward-compatible alias for import rollback."""

        return self.remove_imported_record(doc_id, pack_id=pack_id)

    def update_post_commit_stage(
        self,
        doc_id: str,
        stage: str,
        status: str,
        *,
        error: str = "",
    ) -> bool:
        """Persist the retryable state of work intentionally outside ingest."""

        stage_name = str(stage or "").strip()
        status_name = str(status or "").strip().lower()
        if stage_name not in {"global_chunk_index", "evidence_brain", "wiki"}:
            raise ValueError(f"unsupported paper post-commit stage: {stage_name}")
        if status_name not in {"pending", "running", "completed", "failed", "skipped"}:
            raise ValueError(f"unsupported paper post-commit status: {status_name}")
        with self._file_lock("registry"):
            registry = self._load_registry()
            documents = [
                dict(item)
                for item in registry.get("documents", [])
                if isinstance(item, dict)
            ]
            updated = False
            for entry in documents:
                if str(entry.get("doc_id") or "") != str(doc_id or "").strip():
                    continue
                post_commit = dict(entry.get("post_commit") or {})
                stage_state = dict(post_commit.get(stage_name) or {})
                stage_state.update(
                    {
                        "status": status_name,
                        "updated_at": _utc_now(),
                        "error": str(error or "")[:2000],
                    }
                )
                post_commit[stage_name] = stage_state
                entry["post_commit"] = post_commit
                updated = True
                break
            if updated:
                registry["documents"] = documents
                self._save_registry(registry)
            return updated

    def _find_registry_entry(
        self,
        *,
        sha256: str = "",
        doc_id: str = "",
        pdf_path: str = "",
        pmid: str = "",
        doi: str = "",
        title: str = "",
    ) -> Optional[dict[str, Any]]:
        normalized_doi = str(doi or "").strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")
        normalized_title = re.sub(r"\s+", " ", str(title or "").strip()).casefold()
        registry = self._load_registry()
        for entry in registry.get("documents", []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "ready").strip().lower() != "ready":
                continue
            if sha256 and str(entry.get("sha256") or "") == sha256:
                return dict(entry)
            if doc_id and str(entry.get("doc_id") or "") == doc_id:
                return dict(entry)
            if pdf_path:
                all_paths = [str(item or "") for item in entry.get("all_paths", [])]
                if pdf_path == str(entry.get("pdf_path") or "") or pdf_path in all_paths:
                    return dict(entry)
            identifiers = dict(entry.get("identifiers") or {})
            if pmid and str(identifiers.get("pmid") or entry.get("pmid") or "").strip() == str(pmid).strip():
                return dict(entry)
            entry_doi = str(identifiers.get("doi") or entry.get("doi") or "").strip().lower()
            if normalized_doi and entry_doi == normalized_doi:
                return dict(entry)
            entry_title = re.sub(r"\s+", " ", str(entry.get("title") or "").strip()).casefold()
            if normalized_title and entry_title == normalized_title:
                return dict(entry)
        return None

    def lookup_reference(self, *, pmid: str = "", doi: str = "", title: str = "") -> dict[str, Any]:
        if not any(str(value or "").strip() for value in (pmid, doi, title)):
            raise ValueError("pdf_path or one of pmid, doi, title is required")
        entry = self._find_registry_entry(pmid=pmid, doi=doi, title=title)
        if not entry:
            return {
                "existing": False,
                "pmid": str(pmid or "").strip(),
                "doi": str(doi or "").strip(),
                "title": str(title or "").strip(),
            }
        record_path = str((entry.get("artifacts") or {}).get("record_path") or "")
        record = self._load_record(str(entry.get("doc_id") or "")) or {}
        return {
            "existing": True,
            "doc_id": str(entry.get("doc_id") or ""),
            "sha256": str(entry.get("sha256") or ""),
            "pdf_path": str(entry.get("pdf_path") or ""),
            "title": str(entry.get("title") or ""),
            "record_path": record_path,
            "artifacts": dict(entry.get("artifacts") or {}),
            "fact_counts": dict(entry.get("fact_counts") or {}),
            "visual_counts": dict(entry.get("visual_counts") or {}),
            "l0_summary": dict(entry.get("l0_summary_index") or {}),
            "identifiers": dict(entry.get("identifiers") or {}),
            "needs_reindex": not self._record_supports_layers(record),
            "updated_at": str(entry.get("updated_at") or ""),
        }

    def lookup_pdf(self, pdf_path: str) -> dict[str, Any]:
        path = Path(str(pdf_path or "")).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")

        sha256 = self._compute_sha256(path)
        entry = self._find_registry_entry(sha256=sha256)
        if not entry:
            return {
                "existing": False,
                "sha256": sha256,
                "pdf_path": path.as_posix(),
            }

        record_path_raw = str((entry.get("artifacts") or {}).get("record_path") or "").strip()
        l0_summary = dict(entry.get("l0_summary_index") or {})
        record = self._load_record(str(entry.get("doc_id") or "")) or {}
        return {
            "existing": True,
            "doc_id": str(entry.get("doc_id") or ""),
            "sha256": sha256,
            "pdf_path": str(entry.get("pdf_path") or path.as_posix()),
            "lookup_path": path.as_posix(),
            "title": str(entry.get("title") or ""),
            "record_path": Path(record_path_raw).expanduser().as_posix() if record_path_raw else "",
            "artifacts": dict(entry.get("artifacts") or {}),
            "fact_counts": dict(entry.get("fact_counts") or {}),
            "visual_counts": dict(entry.get("visual_counts") or {}),
            "l0_summary": l0_summary,
            "needs_reindex": not self._record_supports_layers(record),
            "updated_at": str(entry.get("updated_at") or ""),
        }

    def _record_supports_layers(self, record: dict[str, Any]) -> bool:
        if not isinstance(record, dict):
            return False
        required = (
            "l0_summary",
            "l1_passage_summaries",
            "l2_facts",
            "l2_chunks",
            "materialized_views",
            "evidence_anchors",
            "visual_artifacts",
            "doc_chunk_index",
        )
        if not all(key in record for key in required):
            return False
        visual_artifacts = record.get("visual_artifacts")
        if not isinstance(visual_artifacts, dict):
            return False
        doc_chunk_index = record.get("doc_chunk_index")
        if not isinstance(doc_chunk_index, dict):
            return False
        return (
            int(visual_artifacts.get("schema_version") or 0) >= VISUAL_ARTIFACT_SCHEMA_VERSION
            and int(doc_chunk_index.get("schema_version") or 0) >= DOC_CHUNK_INDEX_SCHEMA_VERSION
            and int((record.get("metadata") or {}).get("table_extraction_schema_version") or 0) >= 2
        )

    def _extract_text_payload(self, pdf_path: Path) -> dict[str, Any]:
        extractor_path = (
            Path(__file__).resolve().parent
            / "skills"
            / "pdf"
            / "scripts"
            / "extract_pdf_text.py"
        )
        try:
            spec = importlib.util.spec_from_file_location(
                "paper_research_workflow_pdf_text_extractor",
                extractor_path,
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load text extractor: {extractor_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            payload = module.extract_text(pdf_path)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        pages: list[dict[str, Any]] = []
        full_text_parts: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            text = _normalize_whitespace(page.extract_text() or "")
            pages.append({"page": index, "char_count": len(text), "text": text})
            if text:
                full_text_parts.append(text)
        return {
            "pdf_path": pdf_path.as_posix(),
            "page_count": len(reader.pages),
            "pages": pages,
            "full_text": "\n\n".join(full_text_parts),
        }

    def _extract_tables_payload(self, pdf_path: Path) -> dict[str, Any]:
        plugin_root = Path(__file__).resolve().parent
        extractor_candidates = [
            plugin_root / "runtime" / "pdf" / "extract_pdf_tables.py",
            # Compatibility for deployments that still package the legacy plugin copy.
            plugin_root / "skills" / "pdf" / "scripts" / "extract_pdf_tables.py",
        ]
        extractor_path = next(
            (candidate for candidate in extractor_candidates if candidate.is_file()),
            extractor_candidates[0],
        )
        try:
            spec = importlib.util.spec_from_file_location(
                "paper_research_workflow_pdf_table_extractor",
                extractor_path,
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load table extractor: {extractor_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            payload = module.extract_tables(pdf_path)
            if isinstance(payload, dict):
                return payload
        except Exception:
            # Keep the original extraction path as a compatibility fallback.
            pass

        import pdfplumber
        tables: list[dict[str, Any]] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                found_tables: list[Any] = []
                try:
                    found_tables = list(page.find_tables() or [])
                except Exception:
                    found_tables = []

                for table_index, table in enumerate(page.extract_tables() or [], start=1):
                    table_obj = found_tables[table_index - 1] if table_index <= len(found_tables) else None
                    self._append_table_payload(
                        tables=tables,
                        page_index=page_index,
                        table_index=table_index,
                        table=table,
                        page_width=float(page.width or 0),
                        page_height=float(page.height or 0),
                        bbox=list(getattr(table_obj, "bbox", []) or []) if table_obj is not None else [],
                    )
        return {
            "pdf_path": pdf_path.as_posix(),
            "table_count": len(tables),
            "tables": tables,
        }

    def _append_table_payload(
        self,
        *,
        tables: list[dict[str, Any]],
        page_index: int,
        table_index: int,
        table: Any,
        page_width: float,
        page_height: float,
        bbox: list[Any],
    ) -> None:
        if not table:
            return
        normalized_rows = [_normalize_row(row or []) for row in table]
        rows = [row for row in normalized_rows if any(str(cell or "").strip() for cell in row)]
        if not rows:
            return
        clean_bbox: list[float] = []
        if len(bbox or []) == 4:
            try:
                clean_bbox = [float(value) for value in bbox[:4]]
            except Exception:
                clean_bbox = []
        tables.append(
            {
                "table_id": f"page_{page_index}_table_{table_index}",
                "page": page_index,
                "page_span": [page_index, page_index],
                "table_index": table_index,
                "row_count": len(rows),
                "column_count": max((len(row) for row in rows), default=0),
                "header": rows[0],
                "rows": rows[1:] if len(rows) > 1 else [],
                "matrix": rows,
                "markdown": _table_to_markdown(rows),
                "bbox": clean_bbox,
                "bbox_coordinate_system": "pdfplumber_top_left_points" if clean_bbox else "",
                "page_width": page_width,
                "page_height": page_height,
            }
        )

    def _resize_image_for_visual_manifest(self, image: Any) -> Any:
        max_dim = max(256, int(self.config.visual_image_max_dim or 1600))
        width, height = image.size
        if width <= max_dim and height <= max_dim:
            return image
        scale_factor = min(max_dim / width, max_dim / height)
        new_width = max(1, int(width * scale_factor))
        new_height = max(1, int(height * scale_factor))
        return image.resize((new_width, new_height))

    def _render_page_images(self, pdf_path: Path, doc_dir: Path, page_count: int) -> dict[str, Any]:
        page_images: list[dict[str, Any]] = []
        errors: list[str] = []
        output_dir = doc_dir / "visual" / "page_images"
        if not self.config.render_page_images:
            return {
                "enabled": False,
                "image_dir": output_dir.as_posix(),
                "page_images": page_images,
                "warnings": [],
                "errors": errors,
            }
        dpi = max(72, min(300, int(self.config.visual_image_dpi or 180)))
        max_pages = max(0, int(self.config.visual_max_pages or 0))
        try:
            from pdf2image import convert_from_path
        except Exception as exc:
            return self._render_page_images_with_pymupdf(
                pdf_path=pdf_path,
                output_dir=output_dir,
                dpi=dpi,
                page_count=page_count,
                max_pages=max_pages,
                prior_warnings=[f"pdf2image unavailable, used PyMuPDF fallback: {exc}"],
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"dpi": dpi}
        if max_pages > 0:
            kwargs["last_page"] = min(max_pages, max(1, page_count or max_pages))
        try:
            images = convert_from_path(str(pdf_path), **kwargs)
        except Exception as exc:
            return self._render_page_images_with_pymupdf(
                pdf_path=pdf_path,
                output_dir=output_dir,
                dpi=dpi,
                page_count=page_count,
                max_pages=max_pages,
                prior_warnings=[f"pdf2image page render failed, used PyMuPDF fallback: {exc}"],
            )

        for index, image in enumerate(images, start=1):
            try:
                image = self._resize_image_for_visual_manifest(image)
                image_path = output_dir / f"page_{index}.png"
                image.save(image_path)
                width, height = image.size
                page_images.append(
                    {
                        "page": index,
                        "image_path": image_path.as_posix(),
                        "width": int(width),
                        "height": int(height),
                        "dpi": dpi,
                        "kind": "page_image",
                    }
                )
            except Exception as exc:
                errors.append(f"page {index} image save failed: {exc}")
        return {
            "enabled": True,
            "image_dir": output_dir.as_posix(),
            "page_images": page_images,
            "warnings": [],
            "errors": errors,
        }

    def _render_page_images_with_pymupdf(
        self,
        *,
        pdf_path: Path,
        output_dir: Path,
        dpi: int,
        page_count: int,
        max_pages: int,
        prior_warnings: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        page_images: list[dict[str, Any]] = []
        warnings = list(prior_warnings or [])
        errors: list[str] = []
        try:
            import fitz
        except Exception as exc:
            executable = shutil.which("pdftoppm")
            if not executable and Path("/opt/homebrew/bin/pdftoppm").is_file():
                executable = "/opt/homebrew/bin/pdftoppm"
            if executable:
                import subprocess
                import struct
                output_dir.mkdir(parents=True, exist_ok=True)
                count = min(page_count, max_pages) if max_pages > 0 else page_count
                for number in range(1, count + 1):
                    image_path = output_dir / f"page_{number}.png"
                    try:
                        subprocess.run([executable, "-f", str(number), "-l", str(number), "-singlefile", "-png",
                            "-r", str(dpi), "-scale-to", str(max(256, int(self.config.visual_image_max_dim))),
                            str(pdf_path), str(image_path.with_suffix(""))], check=True, capture_output=True, timeout=45)
                        with image_path.open("rb") as stream:
                            header = stream.read(24)
                        if header[:8] != b"\x89PNG\r\n\x1a\n":
                            raise ValueError("renderer did not produce PNG")
                        width, height = struct.unpack(">II", header[16:24])
                        page_images.append({"page": number, "image_path": image_path.as_posix(), "width": width,
                            "height": height, "dpi": dpi, "kind": "page_image", "renderer": "poppler"})
                    except (OSError, ValueError, subprocess.SubprocessError) as render_error:
                        errors.append(f"page {number} Poppler render failed: {render_error}")
                return {"enabled": True, "image_dir": output_dir.as_posix(), "page_images": page_images,
                    "warnings": warnings + ["Python PDF renderers unavailable; used local Poppler"], "errors": errors}
            errors.append(f"PyMuPDF unavailable: {exc}")
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "page_images": page_images,
                "warnings": warnings,
                "errors": errors,
            }
        try:
            from PIL import Image
        except Exception as exc:
            warnings.append(f"PIL unavailable for PyMuPDF page resize: {exc}")
            Image = None  # type: ignore[assignment]

        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            document = fitz.open(str(pdf_path))
        except Exception as exc:
            errors.append(f"PyMuPDF open failed: {exc}")
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "page_images": page_images,
                "warnings": warnings,
                "errors": errors,
            }

        try:
            total_pages = int(getattr(document, "page_count", 0) or page_count or 0)
            render_count = min(total_pages, max_pages) if max_pages > 0 else total_pages
            zoom = max(1.0, dpi / 72.0)
            matrix = fitz.Matrix(zoom, zoom)
            for page_index in range(render_count):
                page_number = page_index + 1
                try:
                    page = document.load_page(page_index)
                    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                    image_path = output_dir / f"page_{page_number}.png"
                    if Image is not None:
                        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                        image = self._resize_image_for_visual_manifest(image)
                        image.save(image_path)
                        width, height = image.size
                    else:
                        pixmap.save(image_path.as_posix())
                        width, height = pixmap.width, pixmap.height
                    page_images.append(
                        {
                            "page": page_number,
                            "image_path": image_path.as_posix(),
                            "width": int(width),
                            "height": int(height),
                            "dpi": dpi,
                            "kind": "page_image",
                            "renderer": "pymupdf",
                        }
                    )
                except Exception as exc:
                    errors.append(f"page {page_number} PyMuPDF render failed: {exc}")
        finally:
            document.close()

        return {
            "enabled": True,
            "image_dir": output_dir.as_posix(),
            "page_images": page_images,
            "warnings": warnings,
            "errors": errors,
        }

    def _render_table_images(
        self,
        *,
        doc_dir: Path,
        table_payload: dict[str, Any],
        page_images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        table_images: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        output_dir = doc_dir / "visual" / "table_images"
        if not self.config.render_table_images:
            return {
                "enabled": False,
                "image_dir": output_dir.as_posix(),
                "table_images": table_images,
                "warnings": warnings,
                "errors": errors,
            }

        tables = [table for table in (table_payload.get("tables", []) or []) if isinstance(table, dict)]
        if not tables:
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "table_images": table_images,
                "warnings": warnings,
                "errors": errors,
            }

        page_image_by_page = {
            int(item.get("page") or 0): dict(item)
            for item in page_images
            if isinstance(item, dict) and int(item.get("page") or 0) > 0
        }
        if not page_image_by_page:
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "table_images": table_images,
                "warnings": warnings,
                "errors": ["no page images available for table crops"],
            }

        try:
            from PIL import Image
        except Exception as exc:
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "table_images": table_images,
                "warnings": warnings,
                "errors": [f"PIL unavailable: {exc}"],
            }

        output_dir.mkdir(parents=True, exist_ok=True)
        for table in tables:
            table_id = str(table.get("table_id") or "").strip()
            page = int(table.get("page") or 0)
            if not table_id or page <= 0:
                table["image_available"] = False
                continue
            page_image = page_image_by_page.get(page) or {}
            page_image_path = Path(str(page_image.get("image_path") or ""))
            if not page_image_path.is_file():
                table["image_available"] = False
                continue
            table["page_image_path"] = page_image_path.as_posix()
            try:
                table_image_path = output_dir / f"{_safe_slug(table_id, 'table')}_page.png"
                clean_bbox = list(table.get("bbox") or [])
                crop_available = False
                crop_reason = "table bbox unavailable"
                with Image.open(page_image_path) as page_image_obj:
                    page_width_px, page_height_px = page_image_obj.size
                    output_image = page_image_obj.copy()
                    if len(clean_bbox) == 4:
                        page_width_pt = float(table.get("page_width") or 0)
                        page_height_pt = float(table.get("page_height") or 0)
                        if page_width_pt > 0 and page_height_pt > 0:
                            padding = max(0, int(self.config.visual_table_padding_px or 0))
                            scale_x = page_width_px / page_width_pt
                            scale_y = page_height_px / page_height_pt
                            left = max(0, int(float(clean_bbox[0]) * scale_x) - padding)
                            upper = max(0, int(float(clean_bbox[1]) * scale_y) - padding)
                            right = min(page_width_px, int(float(clean_bbox[2]) * scale_x) + padding)
                            lower = min(page_height_px, int(float(clean_bbox[3]) * scale_y) + padding)
                            if right > left and lower > upper:
                                output_image = page_image_obj.crop((left, upper, right, lower))
                                crop_available = True
                                crop_reason = ""
                    output_image.save(table_image_path)
                    image_width, image_height = output_image.size
                table_image = {
                    "table_id": table_id,
                    "page": page,
                    "image_path": table_image_path.as_posix(),
                    "page_image_path": page_image_path.as_posix(),
                    "source_page_image_path": page_image_path.as_posix(),
                    "bbox": list(table.get("bbox") or []),
                    "bbox_coordinate_system": str(table.get("bbox_coordinate_system") or ""),
                    "crop_available": crop_available,
                    "crop_unavailable_reason": crop_reason,
                    "width": int(image_width),
                    "height": int(image_height),
                    "kind": "table_page_image",
                }
                table_images.append(table_image)
                table["image_available"] = True
                table["image_path"] = table_image_path.as_posix()
                table["page_image_path"] = page_image_path.as_posix()
                table["image_kind"] = "table_page_image"
            except Exception as exc:
                table["image_available"] = False
                errors.append(f"table {table_id} page image save failed: {exc}")

        return {
            "enabled": True,
            "image_dir": output_dir.as_posix(),
            "table_images": table_images,
            "warnings": warnings,
            "errors": errors,
        }

    def _render_figure_images(
        self,
        *,
        pdf_path: Path,
        doc_dir: Path,
        page_images: list[dict[str, Any]],
        text_payload: dict[str, Any],
    ) -> dict[str, Any]:
        figure_images: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        output_dir = doc_dir / "visual" / "figure_images"
        if not self.config.render_figure_images:
            return {
                "enabled": False,
                "image_dir": output_dir.as_posix(),
                "figure_images": figure_images,
                "warnings": warnings,
                "errors": errors,
            }

        page_image_by_page = {
            int(item.get("page") or 0): dict(item)
            for item in page_images
            if isinstance(item, dict) and int(item.get("page") or 0) > 0
        }
        if not page_image_by_page:
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "figure_images": figure_images,
                "warnings": warnings,
                "errors": ["no page images available for figure crops"],
            }

        try:
            import fitz
        except Exception as exc:
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "figure_images": figure_images,
                "warnings": warnings,
                "errors": [f"PyMuPDF unavailable for figure extraction: {exc}"],
            }
        try:
            from PIL import Image
        except Exception as exc:
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "figure_images": figure_images,
                "warnings": warnings,
                "errors": [f"PIL unavailable for figure extraction: {exc}"],
            }

        output_dir.mkdir(parents=True, exist_ok=True)
        min_dim = max(1, int(self.config.visual_figure_min_dim or 48))
        min_area = max(1, int(self.config.visual_figure_min_area or 4096))
        seen_rects: set[tuple[int, int, int, int, int]] = set()
        try:
            document = fitz.open(str(pdf_path))
        except Exception as exc:
            return {
                "enabled": True,
                "image_dir": output_dir.as_posix(),
                "figure_images": figure_images,
                "warnings": warnings,
                "errors": [f"PyMuPDF open failed for figure extraction: {exc}"],
            }

        try:
            for page_index in range(int(getattr(document, "page_count", 0) or 0)):
                page_number = page_index + 1
                page_image_meta = page_image_by_page.get(page_number) or {}
                page_image_path = Path(str(page_image_meta.get("image_path") or ""))
                if not page_image_path.is_file():
                    continue
                try:
                    page = document.load_page(page_index)
                    page_rect = page.rect
                    image_entries = list(page.get_images(full=True) or [])
                except Exception as exc:
                    errors.append(f"page {page_number} figure metadata failed: {exc}")
                    continue
                if not image_entries:
                    continue
                try:
                    with Image.open(page_image_path) as page_image_obj:
                        page_width_px, page_height_px = page_image_obj.size
                        scale_x = page_width_px / max(1.0, float(page_rect.width))
                        scale_y = page_height_px / max(1.0, float(page_rect.height))
                        figure_index = 0
                        for image_entry in image_entries:
                            try:
                                xref = int(image_entry[0])
                                rects = list(page.get_image_rects(xref) or [])
                            except Exception:
                                continue
                            for rect in rects:
                                rect_width = float(rect.width)
                                rect_height = float(rect.height)
                                if rect_width < min_dim or rect_height < min_dim:
                                    continue
                                if rect_width * rect_height < min_area:
                                    continue
                                page_area = max(1.0, float(page_rect.width * page_rect.height))
                                if rect_width * rect_height / page_area > 0.92:
                                    continue
                                left = max(0, int(float(rect.x0) * scale_x))
                                upper = max(0, int(float(rect.y0) * scale_y))
                                right = min(page_width_px, int(float(rect.x1) * scale_x))
                                lower = min(page_height_px, int(float(rect.y1) * scale_y))
                                if right <= left or lower <= upper:
                                    continue
                                key = (page_number, left, upper, right, lower)
                                if key in seen_rects:
                                    continue
                                seen_rects.add(key)
                                figure_index += 1
                                figure_id = f"page_{page_number}_figure_{figure_index}"
                                figure_image_path = output_dir / f"{figure_id}.png"
                                crop = page_image_obj.crop((left, upper, right, lower))
                                crop.save(figure_image_path)
                                crop_width, crop_height = crop.size
                                figure_images.append(
                                    {
                                        "figure_id": figure_id,
                                        "page": page_number,
                                        "image_path": figure_image_path.as_posix(),
                                        "page_image_path": page_image_path.as_posix(),
                                        "xref": xref,
                                        "bbox": [
                                            float(rect.x0),
                                            float(rect.y0),
                                            float(rect.x1),
                                            float(rect.y1),
                                        ],
                                        "bbox_coordinate_system": "pymupdf_top_left_points",
                                        "crop_box": [left, upper, right, lower],
                                        "width": int(crop_width),
                                        "height": int(crop_height),
                                        "kind": "figure_image",
                                        "caption_hint": self._figure_caption_hint(text_payload, page_number),
                                    }
                                )
                except Exception as exc:
                    errors.append(f"page {page_number} figure crop failed: {exc}")
        finally:
            document.close()

        return {
            "enabled": True,
            "image_dir": output_dir.as_posix(),
            "figure_images": figure_images,
            "warnings": warnings,
            "errors": errors,
        }

    def _build_visual_artifacts(
        self,
        *,
        pdf_path: Path,
        doc_dir: Path,
        text_payload: dict[str, Any],
        table_payload: dict[str, Any],
        page_count: int,
    ) -> dict[str, Any]:
        manifest_path = doc_dir / "visual_manifest.json"
        for child_name in ("page_images", "table_images", "figure_images"):
            shutil.rmtree(doc_dir / "visual" / child_name, ignore_errors=True)
        page_payload = self._render_page_images(pdf_path, doc_dir, page_count)
        table_payload_result = self._render_table_images(
            doc_dir=doc_dir,
            table_payload=table_payload,
            page_images=list(page_payload.get("page_images") or []),
        )
        figure_payload_result = self._render_figure_images(
            pdf_path=pdf_path,
            doc_dir=doc_dir,
            page_images=list(page_payload.get("page_images") or []),
            text_payload=text_payload,
        )
        return {
            "schema_version": VISUAL_ARTIFACT_SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "visual_manifest_path": manifest_path.as_posix(),
            "page_images_dir": str(page_payload.get("image_dir") or ""),
            "table_images_dir": str(table_payload_result.get("image_dir") or ""),
            "figure_images_dir": str(figure_payload_result.get("image_dir") or ""),
            "page_images": list(page_payload.get("page_images") or []),
            "table_images": list(table_payload_result.get("table_images") or []),
            "figure_images": list(figure_payload_result.get("figure_images") or []),
            "page_image_count": len(list(page_payload.get("page_images") or [])),
            "table_image_count": len(list(table_payload_result.get("table_images") or [])),
            "figure_image_count": len(list(figure_payload_result.get("figure_images") or [])),
            "warnings": [
                *list(page_payload.get("warnings") or []),
                *list(table_payload_result.get("warnings") or []),
                *list(figure_payload_result.get("warnings") or []),
            ],
            "errors": [
                *list(page_payload.get("errors") or []),
                *list(table_payload_result.get("errors") or []),
                *list(figure_payload_result.get("errors") or []),
            ],
        }

    def _page_image_path(self, visual_artifacts: Optional[dict[str, Any]], page: int) -> str:
        if not isinstance(visual_artifacts, dict) or page <= 0:
            return ""
        for item in visual_artifacts.get("page_images", []) or []:
            if isinstance(item, dict) and int(item.get("page") or 0) == page:
                return str(item.get("image_path") or "")
        return ""

    def _table_image_path(self, visual_artifacts: Optional[dict[str, Any]], table_id: str) -> str:
        if not isinstance(visual_artifacts, dict) or not table_id:
            return ""
        for item in visual_artifacts.get("table_images", []) or []:
            if isinstance(item, dict) and str(item.get("table_id") or "") == table_id:
                return str(item.get("image_path") or "")
        return ""

    def _figure_image_paths_for_pages(
        self,
        visual_artifacts: Optional[dict[str, Any]],
        pages: list[int],
    ) -> list[str]:
        if not isinstance(visual_artifacts, dict) or not pages:
            return []
        page_set = {int(page or 0) for page in pages if int(page or 0) > 0}
        paths: list[str] = []
        for item in visual_artifacts.get("figure_images", []) or []:
            if not isinstance(item, dict) or int(item.get("page") or 0) not in page_set:
                continue
            image_path = str(item.get("image_path") or "")
            if image_path and image_path not in paths:
                paths.append(image_path)
        return paths

    def _figure_caption_hint(self, text_payload: Optional[dict[str, Any]], page: int) -> str:
        if not isinstance(text_payload, dict) or page <= 0:
            return ""
        page_text = ""
        for item in text_payload.get("pages", []) or []:
            if isinstance(item, dict) and int(item.get("page") or 0) == page:
                page_text = str(item.get("text") or "")
                break
        if not page_text:
            return ""
        match = FIGURE_CAPTION_RE.search(page_text)
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(0)).strip()[:420]

    def _resolve_title(self, pdf_path: Path, text_payload: dict[str, Any], title: str) -> str:
        explicit = str(title or "").strip()
        if explicit:
            return explicit
        for page in text_payload.get("pages", []):
            if not isinstance(page, dict):
                continue
            text = str(page.get("text") or "").strip()
            if not text:
                continue
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            if first_line:
                return first_line[:240]
        return pdf_path.stem

    def _split_long_text(self, text: str, *, max_chars: int) -> list[str]:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if not raw:
            return []
        if len(raw) <= max_chars:
            return [raw]
        sentences = [part.strip() for part in SENTENCE_SPLIT_RE.split(raw) if part.strip()]
        if len(sentences) <= 1:
            windows: list[str] = []
            start = 0
            step = max(120, max_chars - min(max_chars // 4, 120))
            while start < len(raw):
                piece = raw[start : start + max_chars].strip()
                if piece:
                    windows.append(piece)
                if start + max_chars >= len(raw):
                    break
                start += step
            return windows or [raw]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for sentence in sentences:
            extra = len(sentence) + (1 if current else 0)
            if current and current_len + extra > max_chars:
                chunks.append(" ".join(current).strip())
                current = [sentence]
                current_len = len(sentence)
            else:
                current.append(sentence)
                current_len += extra
        if current:
            chunks.append(" ".join(current).strip())
        return chunks or [raw]

    def _build_text_units(self, text_payload: dict[str, Any]) -> list[dict[str, Any]]:
        """按页构建已清理 PDF 控制码的稳定文本单元。"""

        units: list[dict[str, Any]] = []
        max_unit_chars = max(260, self.config.chunk_max_chars // 2)
        for page_entry in text_payload.get("pages", []):
            if not isinstance(page_entry, dict):
                continue
            page_number = int(page_entry.get("page") or 0)
            raw = _normalize_whitespace(
                strip_pdf_control_codes(page_entry.get("text") or "")
            )
            if not raw:
                continue
            blocks = [block.strip() for block in re.split(r"\n\s*\n", raw) if block.strip()]
            if not blocks:
                blocks = [raw]
            page_units: list[str] = []
            for block in blocks:
                page_units.extend(self._split_long_text(block, max_chars=max_unit_chars))
            for unit_index, unit_text in enumerate(page_units, start=1):
                units.append(
                    {
                        "unit_id": f"page:{page_number}:unit:{unit_index}",
                        "page": page_number,
                        "text": unit_text,
                        "char_count": len(unit_text),
                    }
                )
        return units

    def _overlap_units(self, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        overlap_chars = max(0, int(self.config.chunk_overlap_chars or 0))
        if overlap_chars <= 0 or not units:
            return []
        kept: list[dict[str, Any]] = []
        total = 0
        for unit in reversed(units):
            kept.insert(0, unit)
            total += int(unit.get("char_count") or len(str(unit.get("text") or "")))
            if total >= overlap_chars:
                break
        return kept

    def _make_text_chunk(
        self,
        *,
        doc_id: str,
        title: str,
        pdf_path: str,
        index: int,
        units: list[dict[str, Any]],
        visual_artifacts: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        source_pages = [int(unit.get("page") or 0) for unit in units]
        unique_pages = sorted({page for page in source_pages if page > 0})
        page_image_paths = [
            page_image_path
            for page in unique_pages
            if (page_image_path := self._page_image_path(visual_artifacts, page))
        ]
        figure_image_paths = self._figure_image_paths_for_pages(visual_artifacts, unique_pages)
        source_offsets = [
            {
                "page": int(unit.get("page") or 0),
                "unit_id": str(unit.get("unit_id") or ""),
                "start_char": 0,
                "end_char": len(str(unit.get("text") or "")),
            }
            for unit in units
        ]
        text = "\n\n".join(str(unit.get("text") or "").strip() for unit in units if str(unit.get("text") or "").strip())
        page_span = [min(source_pages), max(source_pages)] if source_pages else [0, 0]
        search_text = "\n".join(part for part in [title, text] if part)
        chunk_id = f"{doc_id}:chunk:{index}"
        return {
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "title": title,
            "pdf_path": pdf_path,
            "kind": "page_chunk",
            "page": page_span[0],
            "page_span": page_span,
            "source_pages": sorted(set(source_pages)),
            "page_image_path": page_image_paths[0] if page_image_paths else "",
            "page_image_paths": page_image_paths,
            "figure_image_paths": figure_image_paths,
            "visual_manifest_path": str((visual_artifacts or {}).get("visual_manifest_path") or ""),
            "source_offsets": source_offsets,
            "text": text,
            "search_text": search_text,
        }

    def _build_text_chunks(
        self,
        *,
        doc_id: str,
        title: str,
        pdf_path: str,
        text_payload: dict[str, Any],
        visual_artifacts: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        units = self._build_text_units(text_payload)
        if not units:
            return []
        chunks: list[dict[str, Any]] = []
        current_units: list[dict[str, Any]] = []
        current_len = 0
        chunk_index = 1
        max_chars = max(260, int(self.config.chunk_max_chars or 900))

        for unit in units:
            unit_text = str(unit.get("text") or "")
            unit_len = len(unit_text)
            unit_page = int(unit.get("page") or 0)
            if not current_units:
                current_units = [unit]
                current_len = unit_len
                continue

            last_page = int(current_units[-1].get("page") or 0)
            page_boundary = unit_page != last_page and not bool(self.config.allow_cross_page_chunks)
            would_overflow = current_len + 2 + unit_len > max_chars
            if page_boundary or would_overflow:
                chunks.append(
                    self._make_text_chunk(
                        doc_id=doc_id,
                        title=title,
                        pdf_path=pdf_path,
                        index=chunk_index,
                        units=current_units,
                        visual_artifacts=visual_artifacts,
                    )
                )
                chunk_index += 1
                if page_boundary:
                    current_units = []
                else:
                    current_units = self._overlap_units(current_units)
                current_len = sum(len(str(item.get("text") or "")) for item in current_units)
            current_units.append(unit)
            current_len += (2 if current_units[:-1] else 0) + unit_len

        if current_units:
            chunks.append(
                self._make_text_chunk(
                    doc_id=doc_id,
                    title=title,
                    pdf_path=pdf_path,
                    index=chunk_index,
                    units=current_units,
                    visual_artifacts=visual_artifacts,
                )
            )
        return chunks

    def _build_table_chunks(
        self,
        *,
        doc_id: str,
        title: str,
        pdf_path: str,
        table_payload: dict[str, Any],
        visual_artifacts: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for table in table_payload.get("tables", []):
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "").strip()
            if not table_id:
                continue
            text = str(table.get("markdown") or "").strip()
            if not text:
                matrix = table.get("matrix") or []
                if isinstance(matrix, list):
                    text = "\n".join(
                        " | ".join(str(cell or "") for cell in row)
                        for row in matrix
                        if isinstance(row, list)
                    )
            page = int(table.get("page") or 0)
            page_image_path = str(table.get("page_image_path") or self._page_image_path(visual_artifacts, page))
            table_image_path = str(table.get("image_path") or self._table_image_path(visual_artifacts, table_id))
            figure_image_paths = self._figure_image_paths_for_pages(visual_artifacts, [page] if page else [])
            search_text = "\n".join(
                part
                for part in [
                    title,
                    f"table_id={table_id}",
                    " ".join(str(cell or "") for cell in table.get("header", []) if str(cell or "").strip()),
                    text,
                ]
                if part
            )
            chunks.append(
                {
                    "chunk_id": f"{doc_id}:table:{table_id}",
                    "doc_id": doc_id,
                    "title": title,
                    "pdf_path": pdf_path,
                    "kind": "table_chunk",
                    "page": page,
                    "page_span": [page, page],
                    "source_pages": [page] if page else [],
                    "page_image_path": page_image_path,
                    "page_image_paths": [page_image_path] if page_image_path else [],
                    "table_image_path": table_image_path,
                    "figure_image_paths": figure_image_paths,
                    "visual_manifest_path": str((visual_artifacts or {}).get("visual_manifest_path") or ""),
                    "source_offsets": [
                        {
                            "page": page,
                            "table_id": table_id,
                            "start_char": 0,
                            "end_char": len(text),
                        }
                    ],
                    "table_id": table_id,
                    "headers": list(table.get("header") or []),
                    "text": text,
                    "search_text": search_text,
                }
            )
        return chunks

    def _build_anchor_map(
        self,
        *,
        doc_id: str,
        title: str,
        pdf_path: str,
        l2_chunks: list[dict[str, Any]],
        table_payload: dict[str, Any],
        visual_artifacts: Optional[dict[str, Any]] = None,
    ) -> dict[str, dict[str, Any]]:
        anchor_map: dict[str, dict[str, Any]] = {}
        for chunk in l2_chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id:
                continue
            anchor_map[chunk_id] = {
                "anchor_id": chunk_id,
                "doc_id": doc_id,
                "title": title,
                "pdf_path": pdf_path,
                "kind": str(chunk.get("kind") or ""),
                "page": int(chunk.get("page") or 0),
                "page_span": list(chunk.get("page_span") or []),
                "chunk_id": chunk_id if str(chunk.get("kind") or "") == "page_chunk" else "",
                "table_id": str(chunk.get("table_id") or ""),
                "page_image_path": str(chunk.get("page_image_path") or ""),
                "page_image_paths": list(chunk.get("page_image_paths") or []),
                "table_image_path": str(chunk.get("table_image_path") or ""),
                "figure_image_paths": list(chunk.get("figure_image_paths") or []),
                "visual_manifest_path": str(chunk.get("visual_manifest_path") or ""),
                "excerpt": str(chunk.get("text") or "")[:320],
            }

        for table in table_payload.get("tables", []):
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "").strip()
            if not table_id:
                continue
            page = int(table.get("page") or 0)
            page_image_path = str(table.get("page_image_path") or self._page_image_path(visual_artifacts, page))
            table_image_path = str(table.get("image_path") or self._table_image_path(visual_artifacts, table_id))
            figure_image_paths = self._figure_image_paths_for_pages(visual_artifacts, [page] if page else [])
            visual_manifest_path = str((visual_artifacts or {}).get("visual_manifest_path") or "")
            rows = table.get("rows") or []
            if not isinstance(rows, list):
                continue
            for row_index, row in enumerate(rows, start=1):
                if not isinstance(row, list):
                    continue
                row_text = " | ".join(str(cell or "").strip() for cell in row if str(cell or "").strip())
                if not row_text:
                    continue
                row_anchor_id = f"{doc_id}:table:{table_id}:row:{row_index}"
                anchor_map[row_anchor_id] = {
                    "anchor_id": row_anchor_id,
                    "doc_id": doc_id,
                    "title": title,
                    "pdf_path": pdf_path,
                    "kind": "table_row",
                    "page": page,
                    "page_span": [page, page],
                    "chunk_id": "",
                    "table_id": table_id,
                    "page_image_path": page_image_path,
                    "page_image_paths": [page_image_path] if page_image_path else [],
                    "table_image_path": table_image_path,
                    "figure_image_paths": figure_image_paths,
                    "visual_manifest_path": visual_manifest_path,
                    "row_index": row_index,
                    "excerpt": row_text[:320],
                }
        return anchor_map

    def _nearest_chunk_anchor(self, *, token: str, chunks: list[dict[str, Any]]) -> str:
        lowered = str(token or "").lower()
        for chunk in chunks:
            if lowered and lowered in str(chunk.get("text") or "").lower():
                return str(chunk.get("chunk_id") or "")
        return str(chunks[0].get("chunk_id") or "") if chunks else ""

    def _gene_candidates(self, title: str, text_payload: dict[str, Any]) -> list[str]:
        """从题名和正文中提取候选基因标识，并先清理 PDF 控制码。"""

        counts: Counter[str] = Counter()
        combined = strip_pdf_control_codes("\n".join(
            [title, *(str(page.get("text") or "") for page in text_payload.get("pages", []) if isinstance(page, dict))]
        ))
        for token in GENE_TOKEN_RE.findall(combined):
            raw = token.strip()
            if len(raw) < 3 or raw.lower() in STOPWORDS:
                continue
            uppercase_count = sum(1 for char in raw if char.isupper())
            if uppercase_count < 2 and not any(char.isdigit() for char in raw):
                continue
            counts[raw] += 1
        return [value for value, _ in counts.most_common(12)]

    def _append_fact(
        self,
        bucket: dict[str, dict[str, Any]],
        *,
        fact_type: str,
        label: str,
        value: str,
        anchor_id: str,
        attributes: Optional[dict[str, Any]] = None,
    ) -> None:
        clean_value = str(value or "").strip()
        clean_label = str(label or "").strip()
        normalized_attrs = dict(attributes or {})
        if not clean_value:
            return
        key = _json_compact(
            {
                "fact_type": fact_type,
                "label": clean_label,
                "value": clean_value,
                "attributes": normalized_attrs,
            }
        )
        item = bucket.setdefault(
            key,
            {
                "fact_type": fact_type,
                "label": clean_label,
                "value": clean_value,
                "attributes": normalized_attrs,
                "anchor_ids": [],
            },
        )
        if anchor_id and anchor_id not in item["anchor_ids"]:
            item["anchor_ids"].append(anchor_id)

    def _extract_l2_facts(
        self,
        *,
        title: str,
        text_payload: dict[str, Any],
        table_payload: dict[str, Any],
        text_chunks: list[dict[str, Any]],
        table_chunks: list[dict[str, Any]],
        anchor_map: dict[str, dict[str, Any]],
        doc_id: str,
    ) -> list[dict[str, Any]]:
        """从正文与表格抽取可追溯事实，并拒绝列关系不可靠的表格指标。

        表格行仍会作为原始证据保留，但只有表头、声明列数和所有正文行
        完全对齐时，才会生成带列名的 measurement、kinetic 或 assay 事实。
        这样可以避免多行表头或空单元格造成的静默列偏移。
        """

        facts: dict[str, dict[str, Any]] = {}

        for gene in self._gene_candidates(title, text_payload):
            self._append_fact(
                facts,
                fact_type="gene_alias",
                label="Gene",
                value=gene,
                anchor_id=self._nearest_chunk_anchor(token=gene, chunks=text_chunks),
            )

        for chunk in [*text_chunks, *table_chunks]:
            chunk_id = str(chunk.get("chunk_id") or "")
            text = strip_pdf_control_codes(chunk.get("text") or "")
            if not text:
                continue
            mutations = find_valid_mutations(text)
            for mutation in mutations:
                self._append_fact(
                    facts,
                    fact_type="mutation",
                    label="Mutation",
                    value=mutation,
                    anchor_id=chunk_id,
                )
            for sentence in _sentence_units(text):
                related_mutations = [
                    mutation
                    for mutation in mutations
                    if re.search(
                        rf"(?<![A-Za-z0-9]){re.escape(mutation)}(?![A-Za-z0-9])",
                        sentence,
                        re.IGNORECASE,
                    )
                ]
                if related_mutations and len(sentence) >= 24:
                    self._append_fact(
                        facts,
                        fact_type="mutation_context",
                        label="Mutation context",
                        value=sentence[:600],
                        anchor_id=chunk_id,
                        attributes={
                            "mutations": related_mutations,
                            "source": "table" if chunk in table_chunks else "text",
                        },
                    )
            if SUBSTRATE_RE.search(text):
                self._append_fact(
                    facts,
                    fact_type="passage_observation",
                    label="Substrate context",
                    value=text[:260],
                    anchor_id=chunk_id,
                    attributes={"source": "text"},
                )
            if KINETIC_RE.search(text):
                metrics = [
                    name
                    for name in ("km", "kcat", "kcat/km", "specific activity", "activity")
                    if name in text.lower()
                ]
                self._append_fact(
                    facts,
                    fact_type="passage_observation",
                    label="Kinetic context",
                    value=text[:260],
                    anchor_id=chunk_id,
                    attributes={"metrics": list(dict.fromkeys(metrics)), "source": "text"},
                )
            if EXPERIMENT_RE.search(text):
                self._append_fact(
                    facts,
                    fact_type="experimental_condition",
                    label="Experimental condition",
                    value=text[:260],
                    anchor_id=chunk_id,
                    attributes={"source": "text"},
                )

        for table in table_payload.get("tables", []):
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "").strip()
            if not table_id:
                continue
            page = int(table.get("page") or 0)
            headers_raw = list(table.get("header") or [])
            headers = [str(cell or "").strip() for cell in headers_raw]
            rows = table.get("rows") or []
            if not isinstance(rows, list):
                rows = []
            declared_column_count = int(table.get("column_count") or len(headers) or 0)
            alignment_metadata = dict(table.get("column_alignment") or {})
            declared_alignment = str(alignment_metadata.get("status") or "").strip().lower()
            aligned_row_lengths = all(
                isinstance(row, list) and len(row) == len(headers)
                for row in rows
            )
            alignment_reliable = bool(
                headers
                and declared_column_count == len(headers)
                and aligned_row_lengths
                and declared_alignment in {"", "aligned"}
            )
            alignment_status = "aligned" if alignment_reliable else "ambiguous"
            for row_index, row in enumerate(rows, start=1):
                if not isinstance(row, list):
                    continue
                row_cells = [strip_pdf_control_codes(cell).strip() for cell in row]
                row_text = " | ".join(cell for cell in row_cells if cell)
                if not row_text:
                    continue
                row_anchor_id = f"{doc_id}:table:{table_id}:row:{row_index}"
                row_attrs = {
                    "table_id": table_id,
                    "row_index": row_index,
                    "page": page,
                    "headers": headers,
                    "table_alignment_status": alignment_status,
                    "expected_column_count": len(headers),
                    "actual_column_count": len(row_cells),
                }
                self._append_fact(
                    facts,
                    fact_type="table_row",
                    label="Table row",
                    value=row_text[:320],
                    anchor_id=row_anchor_id,
                    attributes=row_attrs,
                )
                for mutation in find_valid_mutations(row_text):
                    self._append_fact(
                        facts,
                        fact_type="mutation",
                        label="Mutation",
                        value=mutation,
                        anchor_id=row_anchor_id,
                        attributes={"table_id": table_id, "row_index": row_index},
                    )

                if not alignment_reliable or len(row_cells) != len(headers):
                    continue
                row_object = {
                    headers[idx]: row_cells[idx]
                    for idx in range(len(row_cells))
                    if headers[idx] and row_cells[idx]
                }
                for col_index, cell in enumerate(row_cells):
                    cell_value = str(cell or "").strip()
                    if not cell_value:
                        continue
                    header = headers[col_index]
                    if not header:
                        continue
                    header_lower = header.lower()
                    attributes = {
                        "table_id": table_id,
                        "row_index": row_index,
                        "column_index": col_index + 1,
                        "header": header,
                        "page": page,
                        "row_object": row_object,
                        "table_alignment_status": alignment_status,
                        "expected_column_count": len(headers),
                        "actual_column_count": len(row_cells),
                    }
                    self._append_fact(
                        facts,
                        fact_type="table_measurement",
                        label=header,
                        value=cell_value,
                        anchor_id=row_anchor_id,
                        attributes=attributes,
                    )
                    if "substrate" in header_lower or "compound" in header_lower:
                        self._append_fact(
                            facts,
                            fact_type="compound",
                            label=header or "Substrate",
                            value=cell_value,
                            anchor_id=row_anchor_id,
                            attributes=attributes,
                        )
                    if HEADER_METRIC_RE.search(header_lower):
                        metric_type = "kinetic_metric" if any(
                            token in header_lower for token in ("km", "kcat")
                        ) else "assay_result"
                        self._append_fact(
                            facts,
                            fact_type=metric_type,
                            label=header,
                            value=cell_value,
                            anchor_id=row_anchor_id,
                            attributes=attributes,
                        )
                    if EXPERIMENT_RE.search(header_lower):
                        self._append_fact(
                            facts,
                            fact_type="experimental_condition",
                            label=header,
                            value=cell_value,
                            anchor_id=row_anchor_id,
                            attributes=attributes,
                        )

        items = sorted(
            facts.values(),
            key=lambda item: (
                str(item.get("fact_type") or ""),
                str(item.get("label") or ""),
                str(item.get("value") or ""),
            ),
        )
        finalized: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            search_text = "\n".join(
                part
                for part in [
                    f"fact_type={item.get('fact_type')}",
                    f"label={item.get('label')}",
                    f"value={item.get('value')}",
                    _json_compact(item.get("attributes") or {}),
                ]
                if part
            )
            finalized.append(
                {
                    "fact_id": f"{doc_id}:fact:{index}",
                    "doc_id": doc_id,
                    "fact_type": str(item.get("fact_type") or ""),
                    "label": str(item.get("label") or ""),
                    "value": str(item.get("value") or ""),
                    "attributes": dict(item.get("attributes") or {}),
                    "anchor_ids": list(item.get("anchor_ids") or []),
                    "search_text": search_text,
                }
            )
        return finalized

    def _build_materialized_views(self, facts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        views = {
            "genes": [],
            "mutations": [],
            "substrates": [],
            "activity_metrics": [],
            "kinetics": [],
        }
        seen: dict[str, set[str]] = {key: set() for key in views}

        def push(view_name: str, fact: dict[str, Any]) -> None:
            key = f"{fact.get('value')}|{_json_compact(fact.get('attributes') or {})}"
            if key in seen[view_name]:
                return
            seen[view_name].add(key)
            views[view_name].append(
                {
                    "fact_id": str(fact.get("fact_id") or ""),
                    "label": str(fact.get("label") or ""),
                    "value": str(fact.get("value") or ""),
                    "attributes": dict(fact.get("attributes") or {}),
                    "anchor_ids": list(fact.get("anchor_ids") or []),
                }
            )

        for fact in facts:
            fact_type = str(fact.get("fact_type") or "")
            label = str(fact.get("label") or "").lower()
            if fact_type == "gene_alias":
                push("genes", fact)
            if fact_type == "mutation":
                push("mutations", fact)
            if fact_type == "compound" or "substrate" in label:
                push("substrates", fact)
            if fact_type in {"kinetic_metric", "assay_result"} or any(
                token in label for token in ("km", "kcat", "activity", "yield", "conversion")
            ):
                if fact_type == "kinetic_metric" or any(token in label for token in ("km", "kcat")):
                    push("kinetics", fact)
                else:
                    push("activity_metrics", fact)
            if fact_type == "passage_observation":
                lowered = str(fact.get("value") or "").lower()
                if SUBSTRATE_RE.search(lowered):
                    push("substrates", fact)
                if KINETIC_RE.search(lowered):
                    push("kinetics", fact)
                    push("activity_metrics", fact)
        return views

    def _build_l0_summary(
        self,
        *,
        title: str,
        pdf_path: str,
        text_payload: dict[str, Any],
        table_payload: dict[str, Any],
        l1_passage_summaries: list[dict[str, Any]],
        materialized_views: dict[str, list[dict[str, Any]]],
        l2_facts: list[dict[str, Any]],
        visual_artifacts: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """构建不含 PDF 控制码的文献级摘要、对象和检索字段。"""

        abstract_like = ""
        for summary in l1_passage_summaries:
            if str(summary.get("section_hint") or "") == "abstract":
                abstract_like = str(summary.get("summary_text") or "")
                break
        if not abstract_like:
            abstract_like = _first_sentence(
                strip_pdf_control_codes(text_payload.get("full_text") or ""),
                320,
            )

        primary_objects = []
        for field in ("genes", "mutations", "substrates"):
            for item in list(materialized_views.get(field) or [])[:3]:
                value = str(item.get("value") or "").strip()
                if value and value not in primary_objects:
                    primary_objects.append(value)
        key_findings = []
        for fact in l2_facts:
            fact_type = str(fact.get("fact_type") or "")
            if fact_type in {"kinetic_metric", "assay_result", "mutation", "compound"}:
                value = str(fact.get("value") or "").strip()
                if value and value not in key_findings:
                    key_findings.append(value)
            if len(key_findings) >= 4:
                break

        question_types = []
        for view_name, items in materialized_views.items():
            if items:
                if view_name == "genes":
                    question_types.append("gene")
                elif view_name == "mutations":
                    question_types.append("mutation")
                elif view_name == "substrates":
                    question_types.append("substrate")
                elif view_name in {"activity_metrics", "kinetics"}:
                    question_types.append("kinetics")
        if int(text_payload.get("page_count") or 0) > 0:
            question_types.append("document")
        question_types = list(dict.fromkeys(question_types))

        keywords = _top_keywords(
            "\n".join(
                [
                    title,
                    abstract_like,
                    " ".join(primary_objects),
                    " ".join(key_findings),
                ]
            ),
            limit=10,
        )
        summary_text = "\n".join(
            part
            for part in [
                f"title: {title}",
                f"abstract_summary: {abstract_like}" if abstract_like else "",
                f"primary_objects: {', '.join(primary_objects[:6])}" if primary_objects else "",
                f"key_findings: {' | '.join(key_findings[:4])}" if key_findings else "",
                f"question_types: {', '.join(question_types)}" if question_types else "",
            ]
            if part
        )
        search_text = "\n".join(
            part
            for part in [
                summary_text,
                pdf_path,
                "keywords: " + ", ".join(keywords) if keywords else "",
            ]
            if part
        )
        return {
            "title": title,
            "summary_text": summary_text,
            "abstract_summary": abstract_like,
            "primary_objects": primary_objects[:8],
            "key_findings": key_findings[:6],
            "keywords": keywords,
            "question_types": question_types,
            "coverage": {
                "page_count": int(text_payload.get("page_count") or 0),
                "table_count": int(table_payload.get("table_count") or 0),
                "table_extraction_schema_version": int(table_payload.get("schema_version") or 0),
                "page_image_count": int((visual_artifacts or {}).get("page_image_count") or 0),
                "table_image_count": int((visual_artifacts or {}).get("table_image_count") or 0),
                "figure_image_count": int((visual_artifacts or {}).get("figure_image_count") or 0),
            },
            "page_span": [1, int(text_payload.get("page_count") or 0)],
            "search_text": search_text,
        }

    def _infer_section_hint(self, text: str) -> str:
        snippet = str(text or "")[:200]
        for name, pattern in SECTION_HINTS:
            if pattern.search(snippet):
                return name
        return ""

    def _build_l1_passage_summaries(
        self,
        *,
        doc_id: str,
        title: str,
        pdf_path: str,
        l2_chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for index, chunk in enumerate(l2_chunks, start=1):
            summary_text = _first_sentence(
                str(chunk.get("text") or ""),
                max(120, int(self.config.paragraph_summary_max_chars or 220)),
            )
            question_types = _question_types_from_text(
                "\n".join(
                    [
                        summary_text,
                        str(chunk.get("text") or "")[:300],
                        " ".join(str(item) for item in (chunk.get("headers") or []) if str(item).strip()),
                    ]
                )
            )
            keywords = _top_keywords(
                "\n".join(
                    [
                        summary_text,
                        " ".join(str(item) for item in (chunk.get("headers") or []) if str(item).strip()),
                    ]
                ),
                limit=6,
            )
            search_text = "\n".join(
                part
                for part in [
                    title,
                    summary_text,
                    "question_types: " + ", ".join(question_types) if question_types else "",
                    "keywords: " + ", ".join(keywords) if keywords else "",
                ]
                if part
            )
            summaries.append(
                {
                    "passage_id": f"{doc_id}:passage:{index}",
                    "doc_id": doc_id,
                    "title": title,
                    "pdf_path": pdf_path,
                    "kind": str(chunk.get("kind") or ""),
                    "summary_text": summary_text,
                    "page_span": list(chunk.get("page_span") or []),
                    "source_pages": list(chunk.get("source_pages") or []),
                    "page_image_path": str(chunk.get("page_image_path") or ""),
                    "page_image_paths": list(chunk.get("page_image_paths") or []),
                    "table_image_path": str(chunk.get("table_image_path") or ""),
                    "figure_image_paths": list(chunk.get("figure_image_paths") or []),
                    "visual_manifest_path": str(chunk.get("visual_manifest_path") or ""),
                    "section_hint": self._infer_section_hint(str(chunk.get("text") or "")),
                    "source_chunk_ids": [str(chunk.get("chunk_id") or "")],
                    "question_types": question_types,
                    "keywords": keywords,
                    "search_text": search_text,
                }
            )
        return summaries

    def _collect_used_anchors(
        self,
        *,
        l2_facts: list[dict[str, Any]],
        anchor_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        used: set[str] = set()
        for fact in l2_facts:
            for anchor_id in fact.get("anchor_ids", []) or []:
                if anchor_id in anchor_map:
                    used.add(str(anchor_id))
        return [anchor_map[anchor_id] for anchor_id in sorted(used)]

    def _build_doc_search_text(
        self,
        *,
        title: str,
        pdf_path: str,
        l0_summary: dict[str, Any],
        materialized_views: dict[str, list[dict[str, Any]]],
    ) -> str:
        parts = [title, pdf_path, str(l0_summary.get("summary_text") or "")]
        for field in ("genes", "mutations", "substrates", "activity_metrics", "kinetics"):
            values = materialized_views.get(field) or []
            snippets = []
            for item in values[:12]:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value") or "").strip()
                if value:
                    snippets.append(value)
            if snippets:
                parts.append(f"{field}: {' | '.join(snippets)}")
        keywords = l0_summary.get("keywords") or []
        if isinstance(keywords, list) and keywords:
            parts.append("keywords: " + ", ".join(str(item) for item in keywords[:10]))
        return "\n".join(part for part in parts if part)

    def _fact_counts(
        self,
        *,
        materialized_views: dict[str, list[dict[str, Any]]],
        l2_facts: list[dict[str, Any]],
    ) -> dict[str, int]:
        return {
            "genes": len(materialized_views.get("genes") or []),
            "mutations": len(materialized_views.get("mutations") or []),
            "substrates": len(materialized_views.get("substrates") or []),
            "activity_metrics": len(materialized_views.get("activity_metrics") or []),
            "kinetics": len(materialized_views.get("kinetics") or []),
            "total_facts": len(l2_facts),
        }

    def _paper_evidence_source_id(self, record: dict[str, Any]) -> str:
        metadata = dict(record.get("metadata") or {})
        pmid = str(metadata.get("pmid") or "").strip()
        doi = str(metadata.get("doi") or "").strip()
        if pmid:
            return f"PMID:{pmid}"
        if doi:
            return f"DOI:{doi}"
        return str(record.get("doc_id") or "").strip()

    def _anchor_lookup(self, record: dict[str, Any]) -> dict[str, dict[str, Any]]:
        anchors = record.get("evidence_anchors") or []
        return {
            str(item.get("anchor_id") or ""): dict(item)
            for item in anchors
            if isinstance(item, dict) and str(item.get("anchor_id") or "").strip()
        }

    def _fact_primary_anchor(
        self,
        fact: dict[str, Any],
        anchor_lookup: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        for anchor_id in fact.get("anchor_ids", []) or []:
            anchor = anchor_lookup.get(str(anchor_id or ""))
            if anchor:
                return anchor
        return {}

    def _fact_entities(self, fact: dict[str, Any]) -> list[str]:
        """提取事实的受控实体标签，并过滤 PDF 字形控制码。"""

        entities: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in entities:
                entities.append(text)

        fact_type = str(fact.get("fact_type") or "").strip()
        label = str(fact.get("label") or "").strip()
        value = strip_pdf_control_codes(fact.get("value") or "").strip()
        add(fact_type)
        add(label)
        for mutation in find_valid_mutations(value):
            add(mutation)
        if fact_type in {"kinetic_metric", "assay_result"} or HEADER_METRIC_RE.search(label):
            add("kinetics")
        for token in ("km", "kcat", "activity", "yield", "conversion", "substrate"):
            if token in label.lower() or token in value.lower():
                add(token)
        attrs = dict(fact.get("attributes") or {})
        for key in ("table_id", "header"):
            add(attrs.get(key))
        row_object = attrs.get("row_object")
        if isinstance(row_object, dict):
            for key, raw_value in list(row_object.items())[:12]:
                if HEADER_METRIC_RE.search(str(key or "")):
                    add(str(key))
                for mutation in find_valid_mutations(raw_value):
                    add(mutation)
        return entities[:24]

    def _fact_locator(self, fact: dict[str, Any], anchor: dict[str, Any]) -> str:
        parts: list[str] = []
        page = anchor.get("page") or (fact.get("attributes") or {}).get("page")
        if page:
            parts.append(f"page {page}")
        table_id = anchor.get("table_id") or (fact.get("attributes") or {}).get("table_id")
        if table_id:
            parts.append(f"table {table_id}")
        row_index = anchor.get("row_index") or (fact.get("attributes") or {}).get("row_index")
        if row_index:
            parts.append(f"row {row_index}")
        chunk_id = anchor.get("chunk_id")
        if chunk_id:
            parts.append(f"chunk {chunk_id}")
        return ", ".join(parts)

    def _chunk_locator(self, chunk: dict[str, Any]) -> str:
        parts: list[str] = []
        page_span = list(chunk.get("page_span") or [])
        if len(page_span) >= 2 and page_span[0] and page_span[1]:
            parts.append(f"page {page_span[0]}" if page_span[0] == page_span[1] else f"pages {page_span[0]}-{page_span[1]}")
        elif chunk.get("page"):
            parts.append(f"page {chunk.get('page')}")
        if chunk.get("table_id"):
            parts.append(f"table {chunk.get('table_id')}")
        if chunk.get("chunk_id"):
            parts.append(f"chunk {chunk.get('chunk_id')}")
        return ", ".join(parts)

    def _chunk_entities(self, chunk: dict[str, Any]) -> list[str]:
        """从证据分块提取受控实体，并阻止 OCR 控制码进入索引。"""

        entities: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in entities:
                entities.append(text)

        kind = str(chunk.get("kind") or "").strip()
        text = strip_pdf_control_codes(chunk.get("text") or "")
        add(kind)
        for mutation in find_valid_mutations(text):
            add(mutation)
        for token in ("km", "kcat", "activity", "yield", "conversion", "substrate", "table", "figure"):
            if token in text.lower() or token in kind.lower():
                add(token)
        table_id = str(chunk.get("table_id") or "")
        if table_id:
            add(table_id)
        return entities[:24]

    @staticmethod
    def _select_context_chunks_for_evidence(
        chunks: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Select a bounded, deterministic EvidenceStore context set.

        Table chunks are frequently appended after all page chunks.  Taking a
        simple prefix therefore creates dangling Wiki evidence links for table
        claims.  Select table-bearing chunks first, then retain source order for
        the actual write so record materialization remains deterministic.
        """

        safe_limit = max(1, min(2000, int(limit)))
        candidates = [
            (index, chunk)
            for index, chunk in enumerate(chunks)
            if str(chunk.get("chunk_id") or "").strip()
            and str(chunk.get("text") or "").strip()
        ]
        selected = sorted(
            candidates,
            key=lambda item: (
                0
                if str(item[1].get("kind") or "") == "table_chunk"
                or str(item[1].get("table_id") or "").strip()
                else 1,
                item[0],
            ),
        )[:safe_limit]
        return [chunk for _index, chunk in sorted(selected, key=lambda item: item[0])]

    def _sync_evidence_for_record(
        self,
        record: dict[str, Any],
        *,
        max_records: int = 200,
        max_context_records: int | None = None,
        max_visual_records: int = 120,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Write paper L2 facts into the workspace evidence brain.

        This method is fail-open so sandbox workers without the full tcode
        package can still ingest PDFs. The host process may run this later as a
        bounded background sync after sandbox completion.
        """

        try:
            from tcode_agent.evidence import EvidenceStore, build_evidence_record
        except Exception as exc:
            return record, {
                "enabled": False,
                "error": f"{type(exc).__name__}: {exc}",
                "record_count": 0,
                "evidence_refs": [],
            }

        doc_id = str(record.get("doc_id") or "").strip()
        if not doc_id:
            return record, {
                "enabled": False,
                "error": "missing doc_id",
                "record_count": 0,
                "evidence_refs": [],
            }

        metadata = dict(record.get("metadata") or {})
        title = str(metadata.get("title") or record.get("title") or doc_id)
        source_id = self._paper_evidence_source_id(record)
        pdf_path = str(record.get("pdf_path") or "")
        asset_digests = {pdf_path: str(record['sha256'])} if pdf_path and record.get('sha256') else {}
        artifacts = dict(record.get("artifacts") or {})
        anchor_lookup = self._anchor_lookup(record)
        facts = [dict(item) for item in list(record.get("l2_facts") or []) if isinstance(item, dict)]
        chunks = [dict(item) for item in list(record.get("l2_chunks") or []) if isinstance(item, dict)]
        context_limit = max(
            1,
            min(
                2000,
                int(
                    self.config.evidence_context_max_records
                    if max_context_records is None
                    else max_context_records
                ),
            ),
        )
        selected_context_chunks = self._select_context_chunks_for_evidence(
            chunks,
            limit=context_limit,
        )
        visual_artifacts = dict(record.get("visual_artifacts") or {})
        evidence_refs: list[dict[str, Any]] = []
        context_refs: list[dict[str, Any]] = []
        visual_refs: list[dict[str, Any]] = []
        pending_records: list[Any] = []
        pending_callbacks: list[Any] = []

        def queue_evidence_record(record_payload: Any, callback: Any) -> None:
            pending_records.append(record_payload)
            pending_callbacks.append(callback)

        for index, fact in enumerate(facts, start=1):
            if index > max_records:
                continue
            fact_id = str(fact.get("fact_id") or f"{doc_id}:fact:{index}")
            evidence_id = str(fact.get("evidence_id") or f"paper:{fact_id}")
            fact_type = str(fact.get("fact_type") or "fact")
            label = str(fact.get("label") or fact_type)
            value = str(fact.get("value") or "").strip()
            anchor = self._fact_primary_anchor(fact, anchor_lookup)
            locator = self._fact_locator(fact, anchor)
            anchor_excerpt = str(anchor.get("excerpt") or "").strip()
            content_parts = [f"{label}: {value}" if value else label]
            if anchor_excerpt and anchor_excerpt not in content_parts[0]:
                content_parts.append(f"Evidence excerpt: {anchor_excerpt}")
            record_payload = build_evidence_record(
                evidence_id=evidence_id,
                kind=f"paper_{fact_type}",
                title=f"{title} - {label}",
                content="\n".join(part for part in content_parts if part),
                source_type="paper",
                source_id=source_id,
                source_path=pdf_path,
                locator=locator,
                claims=[f"{label}: {value}" if value else label],
                entities=self._fact_entities(fact),
                citations=[
                    {
                        "doc_id": doc_id,
                        "fact_id": fact_id,
                        "anchor_ids": list(fact.get("anchor_ids") or []),
                        "page": anchor.get("page") or (fact.get("attributes") or {}).get("page"),
                        "table_id": anchor.get("table_id") or (fact.get("attributes") or {}).get("table_id"),
                        "row_index": anchor.get("row_index") or (fact.get("attributes") or {}).get("row_index"),
                    }
                ],
                tags=[
                    "paper",
                    "paper_library",
                    fact_type,
                    doc_id,
                ],
                confidence=0.8,
                workflow_run_id=str(metadata.get("task_id") or ""),
                stage_id="paper_library_ingest",
                artifact_refs={
                    "doc_id": doc_id,
                    "fact_id": fact_id,
                    "anchor_ids": list(fact.get("anchor_ids") or []),
                    "record_path": str(artifacts.get("record_path") or ""),
                    "pdf_path": pdf_path,
                    "tables_extract_path": str(artifacts.get("tables_extract_path") or ""),
                    "visual_manifest_path": str(artifacts.get("visual_manifest_path") or ""),
                },
                source_agent="paper_library_manager",
                metadata={
                    "paper_doc_id": doc_id,
                    "paper_fact": fact,
                    "paper_anchor": anchor,
                    "asset_sha256": asset_digests,
                },
                workdir=self.workdir,
                defer_embedding=True,
            )
            def apply_fact(saved: Any, *, fact: dict[str, Any] = fact, fact_id: str = fact_id, fact_type: str = fact_type, label: str = label) -> None:
                fact["evidence_id"] = saved.evidence_id
                fact["citation_marker"] = f"[evidence:{saved.evidence_id}]"
                evidence_refs.append(
                    {
                        "evidence_id": saved.evidence_id,
                        "citation_marker": f"[evidence:{saved.evidence_id}]",
                        "fact_id": fact_id,
                        "fact_type": fact_type,
                        "label": label,
                        "anchor_ids": list(fact.get("anchor_ids") or []),
                    }
                )

            queue_evidence_record(record_payload, apply_fact)

        for index, chunk in enumerate(selected_context_chunks, start=1):
            chunk_id = str(chunk.get("chunk_id") or f"{doc_id}:chunk:{index}").strip()
            text = str(chunk.get("text") or "").strip()
            if not chunk_id or not text:
                continue
            kind = str(chunk.get("kind") or "page_chunk").strip() or "page_chunk"
            page = int(chunk.get("page") or 0)
            page_span = list(chunk.get("page_span") or [])
            table_id = str(chunk.get("table_id") or "")
            locator = self._chunk_locator(chunk)
            claim = _first_sentence(text, 220) or f"{kind} evidence from {locator or chunk_id}"
            citation: dict[str, Any] = {
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "page": page or None,
                "table_id": table_id or None,
                "text": claim,
            }
            page_image_path = str(chunk.get("page_image_path") or "")
            table_image_path = str(chunk.get("table_image_path") or "")
            if page_image_path:
                citation["image_path"] = page_image_path
            record_payload = build_evidence_record(
                evidence_id=str(chunk.get("evidence_id") or f"paper:{chunk_id}"),
                kind=f"paper_{kind}",
                title=f"{title} - {locator or kind}",
                content=text[:2200],
                source_type="paper",
                source_id=source_id,
                source_path=pdf_path,
                locator=locator,
                claims=[claim],
                entities=self._chunk_entities(chunk),
                citations=[{key: value for key, value in citation.items() if value not in (None, "", [])}],
                tags=[
                    "paper",
                    "paper_library",
                    "paper_chunk",
                    kind,
                    doc_id,
                ],
                confidence=0.72,
                workflow_run_id=str(metadata.get("task_id") or ""),
                stage_id="paper_library_ingest",
                artifact_refs={
                    "doc_id": doc_id,
                    "chunk_id": chunk_id,
                    "record_path": str(artifacts.get("record_path") or ""),
                    "pdf_path": pdf_path,
                    "page_image_path": page_image_path,
                    "page_image_paths": list(chunk.get("page_image_paths") or []),
                    "table_image_path": table_image_path,
                    "figure_image_paths": list(chunk.get("figure_image_paths") or []),
                    "visual_manifest_path": str(artifacts.get("visual_manifest_path") or ""),
                },
                source_agent="paper_library_manager",
                metadata={
                    "paper_doc_id": doc_id,
                    "paper_chunk": chunk,
                    "paper_anchor": anchor_lookup.get(chunk_id, {}),
                    "asset_sha256": asset_digests,
                },
                workdir=self.workdir,
                defer_embedding=True,
            )
            def apply_chunk(
                saved: Any,
                *,
                chunk: dict[str, Any] = chunk,
                chunk_id: str = chunk_id,
                kind: str = kind,
                page: int = page,
                page_span: list[Any] = page_span,
                table_id: str = table_id,
            ) -> None:
                chunk["evidence_id"] = saved.evidence_id
                chunk["citation_marker"] = f"[evidence:{saved.evidence_id}]"
                context_ref = {
                    "evidence_id": saved.evidence_id,
                    "citation_marker": f"[evidence:{saved.evidence_id}]",
                    "chunk_id": chunk_id,
                    "kind": kind,
                    "page": page,
                    "page_span": page_span,
                    "table_id": table_id,
                }
                context_refs.append(context_ref)
                evidence_refs.append(context_ref)

            queue_evidence_record(record_payload, apply_chunk)

        def sync_visual_artifact(item: dict[str, Any], *, visual_kind: str, visual_id: str, label: str) -> None:
            if len(visual_refs) >= max_visual_records:
                return
            image_path = str(item.get("image_path") or "").strip()
            if not image_path:
                return
            page = int(item.get("page") or 0)
            table_id = str(item.get("table_id") or "")
            figure_id = str(item.get("figure_id") or visual_id)
            locator_parts = []
            if page:
                locator_parts.append(f"page {page}")
            if table_id:
                locator_parts.append(f"table {table_id}")
            if visual_kind == "figure_image" and figure_id:
                locator_parts.append(f"figure {figure_id}")
            locator_parts.append(f"image {Path(image_path).name}")
            locator = ", ".join(locator_parts)
            caption_hint = str(item.get("caption_hint") or "").strip()
            content = f"{label} visual evidence for {title}."
            if caption_hint:
                content += f"\nCaption hint: {caption_hint}"
            claim = caption_hint or f"{label} is available as a visual evidence artifact."
            evidence_id = str(item.get("evidence_id") or f"paper:{doc_id}:{visual_kind}:{visual_id}")
            record_payload = build_evidence_record(
                evidence_id=evidence_id,
                kind=f"paper_{visual_kind}",
                title=f"{title} - {label}",
                content=content,
                source_type="paper",
                source_id=source_id,
                source_path=pdf_path,
                locator=locator,
                claims=[claim],
                entities=[
                    visual_kind,
                    "visual_evidence",
                    *([f"page {page}"] if page else []),
                    *([table_id] if table_id else []),
                    *([figure_id] if visual_kind == "figure_image" and figure_id else []),
                ],
                citations=[
                    {
                        "doc_id": doc_id,
                        "page": page or None,
                        "table_id": table_id or None,
                        "figure_id": figure_id if visual_kind == "figure_image" else None,
                        "image_path": image_path,
                        "text": caption_hint,
                    }
                ],
                tags=[
                    "paper",
                    "paper_library",
                    "visual",
                    visual_kind,
                    doc_id,
                ],
                confidence=0.68,
                workflow_run_id=str(metadata.get("task_id") or ""),
                stage_id="paper_library_ingest",
                artifact_refs={
                    "doc_id": doc_id,
                    "record_path": str(artifacts.get("record_path") or ""),
                    "pdf_path": pdf_path,
                    "image_path": image_path,
                    "page_image_path": str(item.get("page_image_path") or ""),
                    "source_page_image_path": str(item.get("source_page_image_path") or ""),
                    "visual_manifest_path": str(artifacts.get("visual_manifest_path") or ""),
                },
                source_agent="paper_library_manager",
                metadata={
                    "paper_doc_id": doc_id,
                    "asset_sha256": asset_digests,
                    "paper_visual_artifact": {
                        **item,
                        "kind": visual_kind,
                    },
                },
                workdir=self.workdir,
                defer_embedding=True,
            )
            def apply_visual(
                saved: Any,
                *,
                item: dict[str, Any] = item,
                visual_kind: str = visual_kind,
                visual_id: str = visual_id,
                page: int = page,
                table_id: str = table_id,
                figure_id: str = figure_id,
                image_path: str = image_path,
            ) -> None:
                item["evidence_id"] = saved.evidence_id
                item["citation_marker"] = f"[evidence:{saved.evidence_id}]"
                visual_ref = {
                    "evidence_id": saved.evidence_id,
                    "citation_marker": f"[evidence:{saved.evidence_id}]",
                    "visual_kind": visual_kind,
                    "visual_id": visual_id,
                    "page": page,
                    "table_id": table_id,
                    "figure_id": figure_id if visual_kind == "figure_image" else "",
                    "image_path": image_path,
                }
                visual_refs.append(visual_ref)
                evidence_refs.append(visual_ref)

            queue_evidence_record(record_payload, apply_visual)

        for item in visual_artifacts.get("page_images", []) or []:
            if not isinstance(item, dict):
                continue
            page = int(item.get("page") or 0)
            if page <= 0:
                continue
            sync_visual_artifact(
                item,
                visual_kind="page_image",
                visual_id=f"page_{page}",
                label=f"Page {page}",
            )
        for item in visual_artifacts.get("table_images", []) or []:
            if not isinstance(item, dict):
                continue
            table_id = str(item.get("table_id") or "").strip()
            if not table_id:
                continue
            sync_visual_artifact(
                item,
                visual_kind="table_image",
                visual_id=table_id,
                label=f"Table {table_id}",
            )
        for item in visual_artifacts.get("figure_images", []) or []:
            if not isinstance(item, dict):
                continue
            figure_id = str(item.get("figure_id") or "").strip()
            if not figure_id:
                continue
            sync_visual_artifact(
                item,
                visual_kind="figure_image",
                visual_id=figure_id,
                label=f"Figure {figure_id}",
            )

        if pending_records:
            try:
                saved_records = EvidenceStore.upsert_many(
                    pending_records,
                    workdir=self.workdir,
                )
            except AttributeError:
                saved_records = [
                    EvidenceStore.upsert(item, workdir=self.workdir)
                    for item in pending_records
                ]
            for saved, callback in zip(saved_records, pending_callbacks):
                callback(saved)

        record["l2_facts"] = facts
        if chunks:
            record["l2_chunks"] = chunks
        if visual_artifacts:
            record["visual_artifacts"] = visual_artifacts
        context_candidate_count = sum(
            1
            for chunk in chunks
            if str(chunk.get("chunk_id") or "").strip()
            and str(chunk.get("text") or "").strip()
        )
        visual_candidate_count = sum(
            1
            for key in ("page_images", "table_images", "figure_images")
            for item in list(visual_artifacts.get(key) or [])
            if isinstance(item, dict) and str(item.get("image_path") or "").strip()
        )
        fact_truncated = len(facts) > max_records
        context_truncated = context_candidate_count > len(selected_context_chunks)
        visual_truncated = visual_candidate_count > max_visual_records
        evidence_summary = {
            "enabled": True,
            "schema_version": EVIDENCE_BRAIN_SCHEMA_VERSION,
            "updated_at": _utc_now(),
            "record_count": len(evidence_refs),
            "total_l2_facts": len(facts),
            "fact_record_count": min(len(facts), max_records),
            "fact_truncated": fact_truncated,
            "context_candidate_count": context_candidate_count,
            "context_limit": context_limit,
            "context_record_count": len(context_refs),
            "context_truncated": context_truncated,
            "visual_candidate_count": visual_candidate_count,
            "visual_record_count": len(visual_refs),
            "visual_truncated": visual_truncated,
            "truncated": fact_truncated or context_truncated or visual_truncated,
            "evidence_refs": evidence_refs,
            "context_refs": context_refs,
            "visual_refs": visual_refs,
        }
        record["evidence_brain"] = evidence_summary
        record_artifacts = dict(record.get("artifacts") or {})
        record_artifacts["evidence_refs"] = evidence_refs
        record_artifacts["context_evidence_refs"] = context_refs
        record_artifacts["visual_evidence_refs"] = visual_refs
        record["artifacts"] = record_artifacts
        return record, evidence_summary

    def _ensure_record_evidence_synced(
        self,
        doc_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        facts = [item for item in list(record.get("l2_facts") or []) if isinstance(item, dict)]
        chunks = [item for item in list(record.get("l2_chunks") or []) if isinstance(item, dict)]
        visual_artifacts = dict(record.get("visual_artifacts") or {})
        visual_items = [
            item
            for key in ("page_images", "table_images", "figure_images")
            for item in list(visual_artifacts.get(key) or [])
            if isinstance(item, dict) and str(item.get("image_path") or "").strip()
        ]
        if not facts and not chunks and not visual_items:
            return record
        first_batch = facts[:200]
        selected_context_chunks = self._select_context_chunks_for_evidence(
            chunks,
            limit=self.config.evidence_context_max_records,
        )
        summary = dict(record.get("evidence_brain") or {})
        if (
            summary.get("enabled")
            and int(summary.get("schema_version") or 0) >= EVIDENCE_BRAIN_SCHEMA_VERSION
            and all(str(fact.get("evidence_id") or "").strip() for fact in first_batch)
            and all(
                str(chunk.get("evidence_id") or "").strip()
                for chunk in selected_context_chunks
            )
            and all(str(item.get("evidence_id") or "").strip() for item in visual_items[:120])
        ):
            return record

        updated_record, sync_summary = self._sync_evidence_for_record(record)
        if sync_summary.get("enabled"):
            self._write_record(str(doc_id or "").strip(), updated_record)
            return updated_record
        return record

    def sync_evidence_for_record(self, doc_id: str) -> dict[str, Any]:
        doc_id_text = str(doc_id or "").strip()
        with self._evidence_sync_lock(doc_id_text):
            record = self._load_record(doc_id_text)
            if not record:
                raise FileNotFoundError(f"Paper library record not found: {doc_id}")
            updated_record, summary = self._sync_evidence_for_record(record)
            if summary.get("enabled"):
                self._write_record(doc_id_text, updated_record)
            return summary

    def ensure_evidence_for_references(
        self,
        doc_id: str,
        *,
        fact_ids: list[str] | None = None,
        chunk_ids: list[str] | None = None,
        max_references: int | None = None,
    ) -> dict[str, Any]:
        """Ensure EvidenceStore rows for Claim source references in one paper.

        Only ``fact_id`` and ``chunk_id`` values present in this document's
        ``record.json`` are eligible.  The caller cannot provide a path or an
        arbitrary Evidence ID, which keeps this Proposal-time repair inside the
        current workspace and paper boundary.
        """

        def unique_strings(values: list[str] | None) -> list[str]:
            result: list[str] = []
            for value in values or []:
                text = str(value or "").strip()
                if text and text not in result:
                    result.append(text)
            return result

        doc_id_text = str(doc_id or "").strip()
        requested_fact_ids = unique_strings(fact_ids)
        requested_chunk_ids = unique_strings(chunk_ids)
        requested = [
            *(("fact", value) for value in requested_fact_ids),
            *(("chunk", value) for value in requested_chunk_ids),
        ]
        safe_limit = max(
            1,
            min(
                2000,
                int(
                    self.config.evidence_ensure_max_references
                    if max_references is None
                    else max_references
                ),
            ),
        )
        selected = requested[:safe_limit]
        omitted = requested[safe_limit:]

        empty_result = {
            "enabled": True,
            "doc_id": doc_id_text,
            "requested_count": len(requested),
            "processed_count": len(selected),
            "max_references": safe_limit,
            "capped": bool(omitted),
            "omitted_fact_ids": [value for kind, value in omitted if kind == "fact"],
            "omitted_chunk_ids": [value for kind, value in omitted if kind == "chunk"],
            "invalid_fact_ids": [],
            "invalid_chunk_ids": [],
            "missing_fact_ids": [],
            "missing_chunk_ids": [],
            "fact_evidence_ids": {},
            "chunk_evidence_ids": {},
            "existing_count": 0,
            "created_count": 0,
            "missing_count": 0,
            "error": "",
        }
        if not selected:
            return empty_result

        try:
            from tcode_agent.evidence import EvidenceStore
        except Exception as exc:
            return {
                **empty_result,
                "enabled": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        with self._evidence_sync_lock(doc_id_text):
            record = self._load_record(doc_id_text)
            if not record:
                raise FileNotFoundError(f"Paper library record not found: {doc_id}")

            facts = [
                dict(item)
                for item in list(record.get("l2_facts") or [])
                if isinstance(item, dict)
            ]
            chunks = [
                dict(item)
                for item in list(record.get("l2_chunks") or [])
                if isinstance(item, dict)
            ]
            record["l2_facts"] = facts
            record["l2_chunks"] = chunks
            fact_lookup = {
                str(item.get("fact_id") or "").strip(): item
                for item in facts
                if str(item.get("fact_id") or "").strip()
            }
            chunk_lookup = {
                str(item.get("chunk_id") or "").strip(): item
                for item in chunks
                if str(item.get("chunk_id") or "").strip()
            }
            selected_fact_ids = [value for kind, value in selected if kind == "fact"]
            selected_chunk_ids = [value for kind, value in selected if kind == "chunk"]
            valid_fact_ids = [value for value in selected_fact_ids if value in fact_lookup]
            valid_chunk_ids = [value for value in selected_chunk_ids if value in chunk_lookup]
            invalid_fact_ids = [value for value in selected_fact_ids if value not in fact_lookup]
            invalid_chunk_ids = [value for value in selected_chunk_ids if value not in chunk_lookup]

            desired_fact_ids = {
                fact_id: f"paper:{fact_id}"
                for fact_id in valid_fact_ids
            }
            desired_chunk_ids = {
                chunk_id: f"paper:{chunk_id}"
                for chunk_id in valid_chunk_ids
            }
            desired_evidence_ids = [
                *desired_fact_ids.values(),
                *desired_chunk_ids.values(),
            ]
            existing_records = EvidenceStore.get_many(
                desired_evidence_ids,
                workdir=self.workdir,
            )

            for fact_id, evidence_id in desired_fact_ids.items():
                if evidence_id in existing_records:
                    fact_lookup[fact_id]["evidence_id"] = evidence_id
                    fact_lookup[fact_id]["citation_marker"] = f"[evidence:{evidence_id}]"
            for chunk_id, evidence_id in desired_chunk_ids.items():
                if evidence_id in existing_records:
                    chunk_lookup[chunk_id]["evidence_id"] = evidence_id
                    chunk_lookup[chunk_id]["citation_marker"] = f"[evidence:{evidence_id}]"

            missing_fact_ids_before = [
                fact_id
                for fact_id, evidence_id in desired_fact_ids.items()
                if evidence_id not in existing_records
            ]
            missing_chunk_ids_before = [
                chunk_id
                for chunk_id, evidence_id in desired_chunk_ids.items()
                if evidence_id not in existing_records
            ]
            sync_error = ""
            if missing_fact_ids_before or missing_chunk_ids_before:
                def without_stale_evidence(item: dict[str, Any]) -> dict[str, Any]:
                    copied = dict(item)
                    copied.pop("evidence_id", None)
                    copied.pop("citation_marker", None)
                    return copied

                subset_record = {
                    **record,
                    "l2_facts": [
                        without_stale_evidence(fact_lookup[fact_id])
                        for fact_id in missing_fact_ids_before
                    ],
                    "l2_chunks": [
                        without_stale_evidence(chunk_lookup[chunk_id])
                        for chunk_id in missing_chunk_ids_before
                    ],
                    "visual_artifacts": {},
                }
                try:
                    synced_subset, sync_summary = self._sync_evidence_for_record(
                        subset_record,
                        max_records=len(missing_fact_ids_before),
                        max_context_records=max(1, len(missing_chunk_ids_before)),
                        max_visual_records=0,
                    )
                    if not sync_summary.get("enabled"):
                        sync_error = str(sync_summary.get("error") or "Evidence sync is disabled")
                    for item in list(synced_subset.get("l2_facts") or []):
                        fact_id = str(item.get("fact_id") or "").strip()
                        if fact_id in fact_lookup and str(item.get("evidence_id") or "").strip():
                            fact_lookup[fact_id]["evidence_id"] = str(item["evidence_id"])
                            fact_lookup[fact_id]["citation_marker"] = str(
                                item.get("citation_marker")
                                or f"[evidence:{item['evidence_id']}]"
                            )
                    for item in list(synced_subset.get("l2_chunks") or []):
                        chunk_id = str(item.get("chunk_id") or "").strip()
                        if chunk_id in chunk_lookup and str(item.get("evidence_id") or "").strip():
                            chunk_lookup[chunk_id]["evidence_id"] = str(item["evidence_id"])
                            chunk_lookup[chunk_id]["citation_marker"] = str(
                                item.get("citation_marker")
                                or f"[evidence:{item['evidence_id']}]"
                            )
                except Exception as exc:
                    sync_error = f"{type(exc).__name__}: {exc}"

            verified_records = EvidenceStore.get_many(
                desired_evidence_ids,
                workdir=self.workdir,
            )
            fact_evidence_ids = {
                fact_id: evidence_id
                for fact_id, evidence_id in desired_fact_ids.items()
                if evidence_id in verified_records
            }
            chunk_evidence_ids = {
                chunk_id: evidence_id
                for chunk_id, evidence_id in desired_chunk_ids.items()
                if evidence_id in verified_records
            }
            missing_fact_ids = [
                fact_id for fact_id in valid_fact_ids if fact_id not in fact_evidence_ids
            ]
            missing_chunk_ids = [
                chunk_id for chunk_id in valid_chunk_ids if chunk_id not in chunk_evidence_ids
            ]

            fact_refs = []
            for fact_id, evidence_id in fact_evidence_ids.items():
                fact = fact_lookup[fact_id]
                fact_refs.append(
                    {
                        "evidence_id": evidence_id,
                        "citation_marker": f"[evidence:{evidence_id}]",
                        "fact_id": fact_id,
                        "fact_type": str(fact.get("fact_type") or "fact"),
                        "label": str(fact.get("label") or fact.get("fact_type") or "fact"),
                        "anchor_ids": list(fact.get("anchor_ids") or []),
                    }
                )
            context_refs = []
            for chunk_id, evidence_id in chunk_evidence_ids.items():
                chunk = chunk_lookup[chunk_id]
                context_refs.append(
                    {
                        "evidence_id": evidence_id,
                        "citation_marker": f"[evidence:{evidence_id}]",
                        "chunk_id": chunk_id,
                        "kind": str(chunk.get("kind") or "page_chunk"),
                        "page": int(chunk.get("page") or 0),
                        "page_span": list(chunk.get("page_span") or []),
                        "table_id": str(chunk.get("table_id") or ""),
                    }
                )

            def merge_refs(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
                merged: dict[str, dict[str, Any]] = {}
                for group in groups:
                    for item in group:
                        if not isinstance(item, dict):
                            continue
                        evidence_id = str(item.get("evidence_id") or "").strip()
                        if evidence_id:
                            merged[evidence_id] = dict(item)
                return list(merged.values())

            previous_summary = dict(record.get("evidence_brain") or {})
            merged_context_refs = merge_refs(
                list(previous_summary.get("context_refs") or []),
                context_refs,
            )
            merged_visual_refs = merge_refs(list(previous_summary.get("visual_refs") or []))
            merged_evidence_refs = merge_refs(
                list(previous_summary.get("evidence_refs") or []),
                fact_refs,
                context_refs,
                merged_visual_refs,
            )
            fact_ref_count = len(
                {
                    str(item.get("fact_id") or "").strip()
                    for item in merged_evidence_refs
                    if str(item.get("fact_id") or "").strip()
                }
            )
            context_candidate_count = sum(
                1
                for item in chunks
                if str(item.get("chunk_id") or "").strip()
                and str(item.get("text") or "").strip()
            )
            visual_candidate_count = int(previous_summary.get("visual_candidate_count") or 0)
            fact_truncated = len(facts) > fact_ref_count
            context_truncated = context_candidate_count > len(merged_context_refs)
            visual_truncated = bool(previous_summary.get("visual_truncated"))
            created_count = sum(
                1
                for evidence_id in [
                    *(desired_fact_ids[fact_id] for fact_id in missing_fact_ids_before),
                    *(desired_chunk_ids[chunk_id] for chunk_id in missing_chunk_ids_before),
                ]
                if evidence_id in verified_records
            )
            ensure_status = {
                "updated_at": _utc_now(),
                "requested_count": len(requested),
                "processed_count": len(selected),
                "max_references": safe_limit,
                "capped": bool(omitted),
                "existing_count": len(existing_records),
                "created_count": created_count,
                "invalid_count": len(invalid_fact_ids) + len(invalid_chunk_ids),
                "missing_count": len(missing_fact_ids) + len(missing_chunk_ids),
                "error": sync_error,
            }
            evidence_summary = {
                **previous_summary,
                "enabled": True,
                "schema_version": EVIDENCE_BRAIN_SCHEMA_VERSION,
                "updated_at": _utc_now(),
                "record_count": len(merged_evidence_refs),
                "total_l2_facts": len(facts),
                "fact_record_count": fact_ref_count,
                "fact_truncated": fact_truncated,
                "context_candidate_count": context_candidate_count,
                "context_limit": int(self.config.evidence_context_max_records),
                "context_record_count": len(merged_context_refs),
                "context_truncated": context_truncated,
                "visual_candidate_count": visual_candidate_count,
                "visual_record_count": len(merged_visual_refs),
                "visual_truncated": visual_truncated,
                "truncated": fact_truncated or context_truncated or visual_truncated,
                "evidence_refs": merged_evidence_refs,
                "context_refs": merged_context_refs,
                "visual_refs": merged_visual_refs,
                "last_on_demand_ensure": ensure_status,
            }
            record["evidence_brain"] = evidence_summary
            artifacts = dict(record.get("artifacts") or {})
            artifacts["evidence_refs"] = merged_evidence_refs
            artifacts["context_evidence_refs"] = merged_context_refs
            artifacts["visual_evidence_refs"] = merged_visual_refs
            record["artifacts"] = artifacts
            self._write_record(doc_id_text, record)

            result = {
                **empty_result,
                "invalid_fact_ids": invalid_fact_ids,
                "invalid_chunk_ids": invalid_chunk_ids,
                "missing_fact_ids": missing_fact_ids,
                "missing_chunk_ids": missing_chunk_ids,
                "fact_evidence_ids": fact_evidence_ids,
                "chunk_evidence_ids": chunk_evidence_ids,
                "existing_count": len(existing_records),
                "created_count": created_count,
                "missing_count": len(missing_fact_ids) + len(missing_chunk_ids),
                "error": sync_error,
            }
            return result

    def _load_record(self, doc_id: str) -> Optional[dict[str, Any]]:
        record_path = self._record_path_for_doc(doc_id)
        payload = _read_json(record_path, None)
        return payload if isinstance(payload, dict) else None

    def _without_embeddings(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return {
                key: self._without_embeddings(value)
                for key, value in payload.items()
                if key != "embedding"
            }
        if isinstance(payload, list):
            return [self._without_embeddings(item) for item in payload]
        return payload

    def _embedding_for_item(self, item: dict[str, Any]) -> list[float]:
        embedding = item.get("embedding")
        if isinstance(embedding, list) and embedding:
            try:
                return [float(value) for value in embedding]
            except Exception:
                pass
        search_text = str(
            item.get("search_text")
            or item.get("summary_text")
            or item.get("text")
            or item.get("value")
            or ""
        )
        return build_embedding(search_text, workdir=self.workdir)

    def _stored_embedding_for_item(self, item: dict[str, Any]) -> list[float]:
        """Return only persisted embeddings to avoid expensive online recompute during retrieval."""
        embedding = item.get("embedding")
        if isinstance(embedding, list) and embedding:
            try:
                return [float(value) for value in embedding]
            except Exception:
                return []
        return []

    def _write_doc_chunk_index(
        self,
        *,
        doc_id: str,
        chunks: list[dict[str, Any]],
        doc_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        base_path = self._doc_chunk_index_base(doc_id, doc_dir=doc_dir)
        index_path = self._faiss_index_path(base_path)
        meta_path = self._faiss_meta_path(base_path)
        payload: dict[str, Any] = {
            "schema_version": DOC_CHUNK_INDEX_SCHEMA_VERSION,
            "doc_chunk_index_base": base_path.as_posix(),
            "doc_chunk_faiss_path": index_path.as_posix(),
            "doc_chunk_faiss_meta_path": meta_path.as_posix(),
            "doc_chunk_index_count": len(chunks),
            "doc_chunk_index_available": False,
            "errors": [],
        }
        if not chunks:
            return payload
        if not FaissVectorStore.available():
            payload["errors"].append("faiss unavailable")
            return payload
        try:
            FaissVectorStore.write(
                base_path=base_path,
                ids=[str(item.get("chunk_id") or "") for item in chunks],
                vectors=[self._embedding_for_item(item) for item in chunks],
            )
            payload["doc_chunk_index_available"] = index_path.is_file() and meta_path.is_file()
        except Exception as exc:
            payload["errors"].append(str(exc))
        return payload

    def _store_record_files(
        self,
        *,
        doc_dir: Path,
        text_payload: dict[str, Any],
        table_payload: dict[str, Any],
        l0_summary: dict[str, Any],
        l1_passage_summaries: list[dict[str, Any]],
        l2_facts: list[dict[str, Any]],
        l2_chunks: list[dict[str, Any]],
        evidence_anchors: list[dict[str, Any]],
        materialized_views: dict[str, list[dict[str, Any]]],
        visual_artifacts: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, str]:
        text_path = doc_dir / "text_extract.json"
        table_path = doc_dir / "tables_extract.json"
        full_text_md_path = doc_dir / "full_text.md"
        l0_summary_path = doc_dir / "l0_summary.json"
        l1_path = doc_dir / "l1_passages.json"
        l2_facts_path = doc_dir / "l2_facts.json"
        l2_chunks_path = doc_dir / "l2_chunks.json"
        anchors_path = doc_dir / "evidence_anchors.json"
        views_path = doc_dir / "materialized_views.json"
        visual_manifest_path = doc_dir / "visual_manifest.json"
        record_path = doc_dir / "record.json"

        _write_json(text_path, text_payload)
        _write_json(table_path, table_payload)
        _write_json(l0_summary_path, self._without_embeddings(l0_summary))
        _write_json(l1_path, self._without_embeddings(l1_passage_summaries))
        _write_json(l2_facts_path, self._without_embeddings(l2_facts))
        _write_json(l2_chunks_path, self._without_embeddings(l2_chunks))
        _write_json(anchors_path, evidence_anchors)
        _write_json(views_path, materialized_views)
        _write_json(visual_manifest_path, visual_artifacts)
        _write_json(record_path, self._without_embeddings(record))
        full_text_md_path.write_text(str(text_payload.get("full_text") or "").strip(), encoding="utf-8")

        return {
            "text_extract_path": text_path.as_posix(),
            "tables_extract_path": table_path.as_posix(),
            "full_text_md_path": full_text_md_path.as_posix(),
            "l0_summary_path": l0_summary_path.as_posix(),
            "l1_passages_path": l1_path.as_posix(),
            "l2_facts_path": l2_facts_path.as_posix(),
            "l2_chunks_path": l2_chunks_path.as_posix(),
            "evidence_anchors_path": anchors_path.as_posix(),
            "materialized_views_path": views_path.as_posix(),
            "visual_manifest_path": visual_manifest_path.as_posix(),
            "page_images_dir": str(visual_artifacts.get("page_images_dir") or ""),
            "table_images_dir": str(visual_artifacts.get("table_images_dir") or ""),
            "figure_images_dir": str(visual_artifacts.get("figure_images_dir") or ""),
            "record_path": record_path.as_posix(),
        }

    def _rebuild_chunk_index(self) -> dict[str, Any]:
        registry = self._load_registry()
        chunks: list[dict[str, Any]] = []
        for entry in registry.get("documents", []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "ready").strip().lower() != "ready":
                continue
            doc_id = str(entry.get("doc_id") or "")
            if not doc_id:
                continue
            record = self._load_record(doc_id)
            if not record:
                continue
            doc_chunks: list[dict[str, Any]] = []
            items = record.get("l2_chunks") or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and str(item.get("chunk_id") or "").strip():
                        normalized_item = dict(item)
                        doc_chunks.append(normalized_item)
                        chunks.append(normalized_item)
            if doc_chunks:
                record_dir = self._record_path_for_doc(doc_id).parent
                doc_index = self._write_doc_chunk_index(
                    doc_id=doc_id,
                    chunks=doc_chunks,
                    doc_dir=record_dir,
                )
                if dict(record.get("doc_chunk_index") or {}) != doc_index:
                    record["doc_chunk_index"] = doc_index
                    artifacts = dict(record.get("artifacts") or {})
                    artifacts.update(doc_index)
                    record["artifacts"] = artifacts
                    self._write_record(doc_id, record)

        payload = {
            "updated_at": _utc_now(),
            "count": len(chunks),
            "chunks": chunks,
        }
        _write_json(self.chunk_index_path, payload)
        if chunks and FaissVectorStore.available():
            try:
                FaissVectorStore.write(
                    base_path=self.chunk_index_base,
                    ids=[str(item.get("chunk_id") or "") for item in chunks],
                    vectors=[self._embedding_for_item(item) for item in chunks],
                )
            except Exception:
                pass
        return payload

    def _reused_ingest_result(
        self,
        *,
        path: Path,
        lookup: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        l1_passage_summaries = list(record.get("l1_passage_summaries") or [])
        l2_facts = list(record.get("l2_facts") or [])
        return {
            "existing": True,
            "reused": True,
            "doc_id": str(lookup.get("doc_id") or record.get("doc_id") or ""),
            "sha256": str(lookup.get("sha256") or record.get("sha256") or ""),
            "pdf_path": str(record.get("pdf_path") or lookup.get("pdf_path") or path.as_posix()),
            "title": str(
                lookup.get("title")
                or (record.get("metadata") or {}).get("title")
                or ""
            ),
            "artifacts": dict(record.get("artifacts") or lookup.get("artifacts") or {}),
            "fact_counts": dict(lookup.get("fact_counts") or {}),
            "record": record,
            "l0_summary": dict(record.get("l0_summary") or {}),
            "visual_artifacts": dict(record.get("visual_artifacts") or {}),
            "l1_passage_count": len(l1_passage_summaries),
            "l2_fact_count": len(l2_facts),
            "evidence_brain": dict(record.get("evidence_brain") or {}),
            "page_span": list((record.get("l0_summary") or {}).get("page_span") or []),
        }

    def _rewrite_staged_json_paths(
        self,
        *,
        staging_doc_dir: Path,
        version_dir: Path,
    ) -> None:
        for json_path in staging_doc_dir.rglob("*.json"):
            payload = _read_json(json_path, None)
            if isinstance(payload, (dict, list)):
                rewritten = _replace_path_prefix(payload, staging_doc_dir, version_dir)
                _write_json(
                    json_path,
                    self._publish_payload_paths(rewritten),
                )

    def _validate_staged_ingest(
        self,
        *,
        staging_doc_dir: Path,
        version_dir: Path,
        doc_id: str,
        sha256: str,
    ) -> dict[str, Any]:
        required_files = (
            "source.pdf",
            "text_extract.json",
            "tables_extract.json",
            "full_text.md",
            "l0_summary.json",
            "l1_passages.json",
            "l2_facts.json",
            "l2_chunks.json",
            "evidence_anchors.json",
            "materialized_views.json",
            "visual_manifest.json",
            "record.json",
            "manifest.json",
        )
        missing = [name for name in required_files if not (staging_doc_dir / name).is_file()]
        if missing:
            raise RuntimeError(f"paper ingest validation failed: missing files: {', '.join(missing)}")
        if self._compute_sha256(staging_doc_dir / "source.pdf") != sha256:
            raise RuntimeError("paper ingest validation failed: source PDF sha256 mismatch")

        record = _read_json(staging_doc_dir / "record.json", None)
        if not isinstance(record, dict):
            raise RuntimeError("paper ingest validation failed: record.json is invalid")
        if str(record.get("doc_id") or "") != doc_id:
            raise RuntimeError("paper ingest validation failed: doc_id mismatch")
        if str(record.get("sha256") or "") != sha256:
            raise RuntimeError("paper ingest validation failed: record sha256 mismatch")
        required_layers: dict[str, type] = {
            "l0_summary": dict,
            "l1_passage_summaries": list,
            "l2_facts": list,
            "l2_chunks": list,
            "materialized_views": dict,
            "evidence_anchors": list,
            "visual_artifacts": dict,
            "doc_chunk_index": dict,
            "artifacts": dict,
        }
        invalid_layers = [
            key
            for key, expected_type in required_layers.items()
            if not isinstance(record.get(key), expected_type)
        ]
        if invalid_layers:
            raise RuntimeError(
                "paper ingest validation failed: invalid layers: "
                + ", ".join(invalid_layers)
            )
        if staging_doc_dir.resolve(strict=False).as_posix() in json.dumps(record, ensure_ascii=False):
            raise RuntimeError("paper ingest validation failed: staged path leaked into record")
        expected_pdf = (version_dir / "source.pdf").resolve(strict=False).as_posix()
        if str(record.get("pdf_path") or "") != expected_pdf:
            raise RuntimeError("paper ingest validation failed: canonical PDF path mismatch")

        artifacts = dict(record.get("artifacts") or {})
        artifact_files = {
            "text_extract_path": "text_extract.json",
            "tables_extract_path": "tables_extract.json",
            "full_text_md_path": "full_text.md",
            "l0_summary_path": "l0_summary.json",
            "l1_passages_path": "l1_passages.json",
            "l2_facts_path": "l2_facts.json",
            "l2_chunks_path": "l2_chunks.json",
            "evidence_anchors_path": "evidence_anchors.json",
            "materialized_views_path": "materialized_views.json",
            "visual_manifest_path": "visual_manifest.json",
            "record_path": "record.json",
            "manifest_path": "manifest.json",
        }
        for key, file_name in artifact_files.items():
            expected = (version_dir / file_name).resolve(strict=False).as_posix()
            if str(artifacts.get(key) or "") != expected:
                raise RuntimeError(f"paper ingest validation failed: invalid artifact path: {key}")
        return record

    def _commit_staged_ingest(
        self,
        *,
        staging_doc_dir: Path,
        version_dir: Path,
        registry_entry: dict[str, Any],
        source_path: Path,
    ) -> None:
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        if version_dir.exists():
            raise FileExistsError(f"paper ingest version already exists: {version_dir}")
        staging_doc_dir.replace(version_dir)
        try:
            with self._file_lock("registry"):
                registry = self._load_registry()
                documents = [
                    dict(item)
                    for item in registry.get("documents", [])
                    if isinstance(item, dict)
                ]
                sha256 = str(registry_entry.get("sha256") or "")
                previous = next(
                    (item for item in documents if str(item.get("sha256") or "") == sha256),
                    {},
                )
                previous_paths = [
                    str(item or "")
                    for item in previous.get("all_paths", [])
                    if str(item or "").strip()
                ]
                final_pdf_path = str(registry_entry.get("pdf_path") or "")
                registry_entry["all_paths"] = list(
                    dict.fromkeys(
                        [*previous_paths, source_path.as_posix(), final_pdf_path]
                    )
                )
                registry_entry["status"] = "ready"
                registry_entry.setdefault(
                    "post_commit",
                    {
                        "global_chunk_index": {"status": "pending", "updated_at": _utc_now(), "error": ""},
                        "evidence_brain": {"status": "pending", "updated_at": _utc_now(), "error": ""},
                        "wiki": {"status": "pending", "updated_at": _utc_now(), "error": ""},
                    },
                )
                registry["documents"] = sorted(
                    [
                        item
                        for item in documents
                        if str(item.get("sha256") or "") != sha256
                    ]
                    + [registry_entry],
                    key=lambda item: str(item.get("updated_at") or ""),
                    reverse=True,
                )
                self._save_registry(registry)
        except BaseException:
            shutil.rmtree(version_dir, ignore_errors=True)
            raise

    def commit_prepared_ingest(
        self,
        *,
        staging_library_root: Path,
        result: dict[str, Any],
        source_path: Path,
        force_reindex: bool = False,
    ) -> dict[str, Any]:
        """校验并发布由隔离容器准备好的文献版本。

        隔离容器只能写普通工作区中的临时文献库，不能直接修改受保护的
        ``.tcode`` 控制面。本方法在宿主进程中复核 PDF 摘要、分层产物、
        规范路径和 registry 条目后，再以原子移动加 registry 锁完成唯一一次
        发布；校验失败时不会污染正式文献库。
        """

        prepared_root = Path(staging_library_root).expanduser().resolve(strict=False)
        source = Path(source_path).expanduser().resolve()
        doc_id = str(result.get("doc_id") or "").strip()
        sha256 = str(result.get("sha256") or "").strip()
        version_id = str(result.get("version_id") or "").strip()
        if not doc_id or not sha256 or not version_id:
            raise RuntimeError("paper ingest validation failed: prepared result identity missing")
        if not source.is_file():
            raise FileNotFoundError(f"PDF not found: {source}")

        with self._file_lock(f"ingest-{sha256}"):
            lookup = self.lookup_pdf(source.as_posix())
            if lookup.get("existing") and not force_reindex:
                existing_record = self._load_record(str(lookup.get("doc_id") or "")) or {}
                if self._record_supports_layers(existing_record):
                    return self._reused_ingest_result(
                        path=source,
                        lookup=lookup,
                        record=existing_record,
                    )

            staging_version_dir = (
                prepared_root
                / "docs"
                / _safe_slug(doc_id, "paper")
                / "versions"
                / _safe_slug(version_id, "version")
            )
            version_dir = self._doc_version_dir(doc_id, version_id)
            published_version_dir = self._published_path(version_dir)
            record = self._validate_staged_ingest(
                staging_doc_dir=staging_version_dir,
                version_dir=published_version_dir,
                doc_id=doc_id,
                sha256=sha256,
            )

            staged_registry = _read_json(prepared_root / "registry.json", {})
            staged_documents = (
                staged_registry.get("documents", [])
                if isinstance(staged_registry, dict)
                else []
            )
            registry_entry = next(
                (
                    dict(item)
                    for item in staged_documents
                    if isinstance(item, dict)
                    and str(item.get("sha256") or "") == sha256
                ),
                None,
            )
            if not registry_entry:
                raise RuntimeError("paper ingest validation failed: prepared registry entry missing")
            registry_entry["version_id"] = version_id
            registry_entry["pdf_path"] = str(record.get("pdf_path") or "")
            registry_entry["artifacts"] = dict(record.get("artifacts") or {})
            registry_entry["record_path"] = str(
                (record.get("artifacts") or {}).get("record_path") or ""
            )
            registry_entry["updated_at"] = _utc_now()

            self._commit_staged_ingest(
                staging_doc_dir=staging_version_dir,
                version_dir=version_dir,
                registry_entry=registry_entry,
                source_path=source,
            )

            committed = dict(result)
            committed["existing"] = False
            committed["reused"] = False
            committed["record"] = self._load_record(doc_id) or record
            committed["artifacts"] = dict(committed["record"].get("artifacts") or {})
            committed["pdf_path"] = str(committed["record"].get("pdf_path") or "")
            try:
                chunk_index = self._rebuild_chunk_index()
                committed["chunk_index_count"] = int(chunk_index.get("count") or 0)
                self.update_post_commit_stage(doc_id, "global_chunk_index", "completed")
            except Exception as exc:
                try:
                    self.update_post_commit_stage(
                        doc_id,
                        "global_chunk_index",
                        "failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    pass
                warnings = list(committed.get("post_commit_warnings") or [])
                warnings.append(
                    f"host global chunk index rebuild failed: {type(exc).__name__}: {exc}"
                )
                committed["post_commit_warnings"] = warnings
            return committed

    def ingest_pdf(
        self,
        pdf_path: str,
        *,
        task_id: str = "",
        title: str = "",
        pmid: str = "",
        doi: str = "",
        source_url: str = "",
        text_payload_override: Optional[Any] = None,
        table_payload_override: Optional[Any] = None,
        force_reindex: bool = False,
        ingest_attempt_id: str = "",
    ) -> dict[str, Any]:
        """Build one paper privately and publish it with one registry commit."""

        path = Path(str(pdf_path or "")).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")
        sha256 = self._compute_sha256(path)
        doc_id = self._doc_id(sha256)

        with self._file_lock(f"ingest-{sha256}"):
            lookup = self.lookup_pdf(path.as_posix())
            if lookup.get("existing") and not force_reindex:
                existing_record = self._load_record(str(lookup.get("doc_id") or "")) or {}
                if self._record_supports_layers(existing_record):
                    return self._reused_ingest_result(
                        path=path,
                        lookup=lookup,
                        record=existing_record,
                    )

            attempt_id = _safe_slug(ingest_attempt_id, uuid.uuid4().hex)
            version_id = (
                datetime.now(timezone.utc).strftime("v-%Y%m%dT%H%M%S%fZ-")
                + attempt_id[:8]
            )
            staging_library_root = self.staging_root / attempt_id
            staging_store = PaperLibraryStore(
                library_root=staging_library_root,
                workdir=self.workdir,
                config=self.config,
            )
            version_dir = self._doc_version_dir(doc_id, version_id)
            published_version_dir = self._published_path(version_dir)
            try:
                staged_result = staging_store._ingest_pdf_direct(
                    path.as_posix(),
                    task_id=task_id,
                    title=title,
                    pmid=pmid,
                    doi=doi,
                    source_url=source_url,
                    text_payload_override=text_payload_override,
                    table_payload_override=table_payload_override,
                    force_reindex=False,
                    sync_evidence=False,
                    rebuild_global_index=False,
                )
                staging_doc_dir = staging_store._doc_dir(doc_id)
                self._rewrite_staged_json_paths(
                    staging_doc_dir=staging_doc_dir,
                    version_dir=published_version_dir,
                )
                result = _replace_path_prefix(
                    staged_result,
                    staging_doc_dir,
                    published_version_dir,
                )
                result = self._publish_payload_paths(result)
                record = dict(result.get("record") or {})
                artifacts = dict(record.get("artifacts") or result.get("artifacts") or {})
                artifacts["manifest_path"] = (published_version_dir / "manifest.json").as_posix()
                record["artifacts"] = artifacts
                record["pdf_path"] = (published_version_dir / "source.pdf").as_posix()
                record["chunk_index_ref"] = self._published_path(self.chunk_index_path).as_posix()
                metadata = dict(record.get("metadata") or {})
                metadata["library_version_id"] = version_id
                metadata["ingest_attempt_id"] = attempt_id
                record["metadata"] = metadata
                result["record"] = record
                result["artifacts"] = artifacts
                result["pdf_path"] = record["pdf_path"]
                result["version_id"] = version_id
                result["ingest_transaction"] = {
                    "attempt_id": attempt_id,
                    "version_id": version_id,
                    "status": "ready",
                    "commit_model": "staging_registry_atomic_publish",
                }
                _write_json(staging_doc_dir / "record.json", record)
                _write_json(
                    staging_doc_dir / "manifest.json",
                    {
                        "schema_version": 1,
                        "doc_id": doc_id,
                        "sha256": sha256,
                        "version_id": version_id,
                        "attempt_id": attempt_id,
                        "status": "prepared",
                        "created_at": _utc_now(),
                        "record_path": (published_version_dir / "record.json").as_posix(),
                    },
                )
                self._validate_staged_ingest(
                    staging_doc_dir=staging_doc_dir,
                    version_dir=published_version_dir,
                    doc_id=doc_id,
                    sha256=sha256,
                )

                staged_registry = staging_store._load_registry()
                staged_entry = next(
                    (
                        dict(item)
                        for item in staged_registry.get("documents", [])
                        if isinstance(item, dict)
                        and str(item.get("sha256") or "") == sha256
                    ),
                    None,
                )
                if not staged_entry:
                    raise RuntimeError("paper ingest validation failed: staged registry entry missing")
                registry_entry = _replace_path_prefix(
                    staged_entry,
                    staging_doc_dir,
                    published_version_dir,
                )
                registry_entry = self._publish_payload_paths(registry_entry)
                registry_entry["version_id"] = version_id
                registry_entry["pdf_path"] = record["pdf_path"]
                registry_entry["artifacts"] = artifacts
                registry_entry["record_path"] = artifacts["record_path"]
                registry_entry["updated_at"] = _utc_now()
                self._commit_staged_ingest(
                    staging_doc_dir=staging_doc_dir,
                    version_dir=version_dir,
                    registry_entry=registry_entry,
                    source_path=self._published_path(path),
                )

                if not self.path_rewrites:
                    try:
                        chunk_index = self._rebuild_chunk_index()
                        result["chunk_index_count"] = int(chunk_index.get("count") or 0)
                        result["record"] = self._load_record(doc_id) or record
                        self.update_post_commit_stage(doc_id, "global_chunk_index", "completed")
                    except Exception as exc:
                        try:
                            self.update_post_commit_stage(
                                doc_id,
                                "global_chunk_index",
                                "failed",
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        except Exception:
                            pass
                        result["post_commit_warnings"] = [
                            f"global chunk index rebuild failed: {type(exc).__name__}: {exc}"
                        ]
                else:
                    result["post_commit_warnings"] = [
                        "global chunk index rebuild deferred to the host process"
                    ]
                return result
            finally:
                shutil.rmtree(staging_library_root, ignore_errors=True)

    def _ingest_pdf_direct(
        self,
        pdf_path: str,
        *,
        task_id: str = "",
        title: str = "",
        pmid: str = "",
        doi: str = "",
        source_url: str = "",
        text_payload_override: Optional[Any] = None,
        table_payload_override: Optional[Any] = None,
        force_reindex: bool = False,
        sync_evidence: bool = True,
        rebuild_global_index: bool = True,
    ) -> dict[str, Any]:
        path = Path(str(pdf_path or "")).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")

        lookup = self.lookup_pdf(path.as_posix())
        if lookup.get("existing") and not force_reindex:
            existing_record = self._load_record(str(lookup.get("doc_id") or "")) or {}
            if self._record_supports_layers(existing_record):
                l1_passage_summaries = list(existing_record.get("l1_passage_summaries") or [])
                l2_facts = list(existing_record.get("l2_facts") or [])
                evidence_summary: dict[str, Any] = {}
                if sync_evidence:
                    existing_record, evidence_summary = self._sync_evidence_for_record(existing_record)
                if evidence_summary.get("enabled"):
                    self._write_record(str(lookup.get("doc_id") or ""), existing_record)
                return {
                    "existing": True,
                    "reused": True,
                    "doc_id": str(lookup.get("doc_id") or ""),
                    "sha256": str(lookup.get("sha256") or ""),
                    "pdf_path": str(existing_record.get("pdf_path") or lookup.get("pdf_path") or path.as_posix()),
                    "title": str(lookup.get("title") or ""),
                    "artifacts": dict(lookup.get("artifacts") or {}),
                    "fact_counts": dict(lookup.get("fact_counts") or {}),
                    "record": existing_record,
                    "l0_summary": dict(existing_record.get("l0_summary") or {}),
                    "visual_artifacts": dict(existing_record.get("visual_artifacts") or {}),
                    "l1_passage_count": len(l1_passage_summaries),
                    "l2_fact_count": len(l2_facts),
                    "evidence_brain": dict(existing_record.get("evidence_brain") or evidence_summary),
                    "page_span": list((existing_record.get("l0_summary") or {}).get("page_span") or []),
                }

        sha256 = str(lookup.get("sha256") or self._compute_sha256(path))
        doc_id = self._doc_id(sha256)
        doc_dir = self._doc_dir(doc_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        canonical_pdf_path = doc_dir / "source.pdf"
        if path != canonical_pdf_path:
            shutil.copy2(path, canonical_pdf_path)
        processing_path = canonical_pdf_path.resolve()

        text_payload = text_payload_override if isinstance(text_payload_override, dict) else None
        if text_payload is None:
            text_payload = self._extract_text_payload(processing_path)
        table_payload = table_payload_override if isinstance(table_payload_override, dict) else None
        if table_payload is None:
            table_payload = self._extract_tables_payload(processing_path)

        text_payload["pdf_path"] = processing_path.as_posix()
        table_payload["pdf_path"] = processing_path.as_posix()

        resolved_title = self._resolve_title(path, text_payload, title)
        full_text = str(text_payload.get("full_text") or "")
        doi_match = DOI_RE.search(full_text)
        pmid_match = PMID_RE.search(full_text)
        resolved_doi = (
            str(doi or "").strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")
            or (doi_match.group(0).rstrip(".,;)").lower() if doi_match else "")
        )
        resolved_pmid = str(pmid or (pmid_match.group(1) if pmid_match else "")).strip()
        identifiers = {
            "pmid": resolved_pmid,
            "doi": resolved_doi,
            "source_url": str(source_url or "").strip(),
        }
        visual_artifacts = self._build_visual_artifacts(
            pdf_path=processing_path,
            doc_dir=doc_dir,
            text_payload=text_payload,
            table_payload=table_payload,
            page_count=int(text_payload.get("page_count") or 0),
        )

        text_chunks = self._build_text_chunks(
            doc_id=doc_id,
            title=resolved_title,
            pdf_path=processing_path.as_posix(),
            text_payload=text_payload,
            visual_artifacts=visual_artifacts,
        )
        table_chunks = self._build_table_chunks(
            doc_id=doc_id,
            title=resolved_title,
            pdf_path=processing_path.as_posix(),
            table_payload=table_payload,
            visual_artifacts=visual_artifacts,
        )
        l2_chunks = [*text_chunks, *table_chunks]
        doc_chunk_index = self._write_doc_chunk_index(doc_id=doc_id, chunks=l2_chunks)
        anchor_map = self._build_anchor_map(
            doc_id=doc_id,
            title=resolved_title,
            pdf_path=processing_path.as_posix(),
            l2_chunks=l2_chunks,
            table_payload=table_payload,
            visual_artifacts=visual_artifacts,
        )
        l2_facts = self._extract_l2_facts(
            title=resolved_title,
            text_payload=text_payload,
            table_payload=table_payload,
            text_chunks=text_chunks,
            table_chunks=table_chunks,
            anchor_map=anchor_map,
            doc_id=doc_id,
        )
        materialized_views = self._build_materialized_views(l2_facts)
        l1_passage_summaries = self._build_l1_passage_summaries(
            doc_id=doc_id,
            title=resolved_title,
            pdf_path=processing_path.as_posix(),
            l2_chunks=l2_chunks,
        )
        l0_summary = self._build_l0_summary(
            title=resolved_title,
            pdf_path=processing_path.as_posix(),
            text_payload=text_payload,
            table_payload=table_payload,
            l1_passage_summaries=l1_passage_summaries,
            materialized_views=materialized_views,
            l2_facts=l2_facts,
            visual_artifacts=visual_artifacts,
        )
        evidence_anchors = self._collect_used_anchors(l2_facts=l2_facts, anchor_map=anchor_map)

        record = {
            "doc_id": doc_id,
            "sha256": sha256,
            "pdf_path": processing_path.as_posix(),
            "metadata": {
                "title": resolved_title,
                "file_name": path.name,
                "original_pdf_path": path.as_posix(),
                "pmid": resolved_pmid,
                "doi": resolved_doi,
                "source_url": str(source_url or "").strip(),
                "page_count": int(text_payload.get("page_count") or 0),
                "table_count": int(table_payload.get("table_count") or 0),
                "table_extraction_schema_version": int(table_payload.get("schema_version") or 0),
                "page_image_count": int(visual_artifacts.get("page_image_count") or 0),
                "table_image_count": int(visual_artifacts.get("table_image_count") or 0),
                "figure_image_count": int(visual_artifacts.get("figure_image_count") or 0),
                "visual_error_count": len(list(visual_artifacts.get("errors") or [])),
                "task_id": str(task_id or "").strip(),
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            },
            "raw_artifacts": {},
            "l0_summary": l0_summary,
            "l1_passage_summaries": l1_passage_summaries,
            "l2_facts": l2_facts,
            "l2_chunks": l2_chunks,
            "materialized_views": materialized_views,
            "evidence_anchors": evidence_anchors,
            "visual_artifacts": visual_artifacts,
            "doc_chunk_index": doc_chunk_index,
            "chunk_index_ref": self.chunk_index_path.as_posix(),
        }
        artifacts = self._store_record_files(
            doc_dir=doc_dir,
            text_payload=text_payload,
            table_payload=table_payload,
            l0_summary=l0_summary,
            l1_passage_summaries=l1_passage_summaries,
            l2_facts=l2_facts,
            l2_chunks=l2_chunks,
            evidence_anchors=evidence_anchors,
            materialized_views=materialized_views,
            visual_artifacts=visual_artifacts,
            record=record,
        )
        artifacts.update(doc_chunk_index)
        artifacts["source_pdf_path"] = processing_path.as_posix()
        record["artifacts"] = artifacts
        record["doc_chunk_index"] = doc_chunk_index
        record["raw_artifacts"] = {
            "text_extract_path": artifacts["text_extract_path"],
            "tables_extract_path": artifacts["tables_extract_path"],
            "full_text_md_path": artifacts["full_text_md_path"],
            "visual_manifest_path": artifacts["visual_manifest_path"],
            "page_images_dir": artifacts["page_images_dir"],
            "table_images_dir": artifacts["table_images_dir"],
            "figure_images_dir": artifacts["figure_images_dir"],
        }
        record = self._without_embeddings(record)
        evidence_summary: dict[str, Any] = {}
        if sync_evidence:
            record, evidence_summary = self._sync_evidence_for_record(record)
        artifacts = dict(record.get("artifacts") or artifacts)
        _write_json(doc_dir / "record.json", record)

        fact_counts = self._fact_counts(materialized_views=materialized_views, l2_facts=l2_facts)
        registry = self._load_registry()
        documents = [item for item in registry.get("documents", []) if isinstance(item, dict)]
        updated_documents: list[dict[str, Any]] = []
        search_text = self._build_doc_search_text(
            title=resolved_title,
            pdf_path=processing_path.as_posix(),
            l0_summary=l0_summary,
            materialized_views=materialized_views,
        )
        search_text = "\n".join(
            value
            for value in (
                search_text,
                f"PMID: {resolved_pmid}" if resolved_pmid else "",
                f"DOI: {resolved_doi}" if resolved_doi else "",
            )
            if value
        )
        registry_entry = {
            "doc_id": doc_id,
            "sha256": sha256,
            "pdf_path": processing_path.as_posix(),
            "all_paths": list(dict.fromkeys([processing_path.as_posix(), path.as_posix()])),
            "title": resolved_title,
            "identifiers": identifiers,
            "status": "ready",
            "updated_at": _utc_now(),
            "artifacts": artifacts,
            "page_span": list(l0_summary.get("page_span") or []),
            "chunk_count": len(l2_chunks),
            "passage_count": len(l1_passage_summaries),
            "fact_counts": fact_counts,
            "visual_counts": {
                "page_images": int(visual_artifacts.get("page_image_count") or 0),
                "table_images": int(visual_artifacts.get("table_image_count") or 0),
                "figure_images": int(visual_artifacts.get("figure_image_count") or 0),
                "errors": len(list(visual_artifacts.get("errors") or [])),
            },
            "l0_summary_index": {
                "summary_text": str(l0_summary.get("summary_text") or ""),
                "keywords": list(l0_summary.get("keywords") or []),
                "question_types": list(l0_summary.get("question_types") or []),
                "page_span": list(l0_summary.get("page_span") or []),
            },
            "search_text": search_text,
        }

        found = False
        for entry in documents:
            if str(entry.get("sha256") or "") == sha256:
                existing_paths = [str(item or "") for item in entry.get("all_paths", []) if str(item or "").strip()]
                if path.as_posix() not in existing_paths:
                    existing_paths.append(path.as_posix())
                merged = dict(registry_entry)
                merged["all_paths"] = existing_paths
                updated_documents.append(merged)
                found = True
            else:
                updated_documents.append(entry)
        if not found:
            updated_documents.append(registry_entry)

        registry["documents"] = sorted(
            updated_documents,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        self._save_registry(registry)
        chunk_index = self._rebuild_chunk_index() if rebuild_global_index else {"count": 0}

        return {
            "existing": False,
            "reused": False,
            "doc_id": doc_id,
            "sha256": sha256,
            "pdf_path": processing_path.as_posix(),
            "title": resolved_title,
            "artifacts": artifacts,
            "fact_counts": fact_counts,
            "record": record,
            "l0_summary": l0_summary,
            "visual_artifacts": visual_artifacts,
            "l1_passage_count": len(l1_passage_summaries),
            "l2_fact_count": len(l2_facts),
            "evidence_brain": dict(record.get("evidence_brain") or evidence_summary),
            "page_span": list(l0_summary.get("page_span") or []),
            "chunk_index_count": int(chunk_index.get("count") or 0),
        }

    def get_record(self, doc_id: str, *, sync_evidence: bool = True) -> dict[str, Any]:
        doc_id_text = str(doc_id or "").strip()
        with self._evidence_sync_lock(doc_id_text):
            record = self._load_record(doc_id_text)
            if not record:
                raise FileNotFoundError(f"Paper library record not found: {doc_id}")
            if not sync_evidence:
                return record
            return self._ensure_record_evidence_synced(doc_id_text, record)

    def _query_categories(self, question: str) -> set[str]:
        categories = set(_question_types_from_text(question))
        lower = question.lower()
        if any(token in lower for token in ("gene", "protein", "enzyme", "基因", "蛋白", "酶")):
            categories.add("gene")
        if any(token in lower for token in ("mutation", "mutant", "variant", "突变")):
            categories.add("mutation")
        if any(token in lower for token in ("km", "kcat", "kinetic", "动力学", "活性")):
            categories.add("kinetics")
        if any(token in lower for token in ("substrate", "底物")):
            categories.add("substrate")
        return categories

    def _fact_materialized_fields(self, fact: dict[str, Any]) -> set[str]:
        fields: set[str] = set()
        label = str(fact.get("label") or "").lower()
        fact_type = str(fact.get("fact_type") or "")
        if fact_type == "gene_alias":
            fields.add("genes")
        if fact_type == "mutation":
            fields.add("mutations")
        if fact_type == "compound" or "substrate" in label:
            fields.add("substrates")
        if fact_type == "kinetic_metric" or any(token in label for token in ("km", "kcat")):
            fields.add("kinetics")
        if fact_type == "assay_result" or any(
            token in label for token in ("activity", "yield", "conversion")
        ):
            fields.add("activity_metrics")
        return fields

    def _requested_materialized_fields(self, question: str) -> list[str]:
        query_categories = self._query_categories(question)
        requested_fields: list[str] = []
        if "gene" in query_categories:
            requested_fields.append("genes")
        if "mutation" in query_categories:
            requested_fields.append("mutations")
        if "substrate" in query_categories:
            requested_fields.append("substrates")
        if "kinetics" in query_categories:
            requested_fields.extend(["kinetics", "activity_metrics"])
        return requested_fields

    def _candidate_documents(
        self,
        *,
        question: str,
        doc_id: str = "",
        pdf_path: str = "",
        allowed_doc_ids: Iterable[str] | None = None,
        limit_docs: int = 0,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        registry = self._load_registry()
        allowed = (
            {str(item or "").strip() for item in allowed_doc_ids if str(item or "").strip()}
            if allowed_doc_ids is not None
            else None
        )
        docs = [
            dict(item)
            for item in registry.get("documents", [])
            if isinstance(item, dict)
            and str(item.get("status") or "ready").strip().lower() == "ready"
            and (allowed is None or str(item.get("doc_id") or "") in allowed)
        ]
        if doc_id:
            docs = [item for item in docs if str(item.get("doc_id") or "") == doc_id]
        if pdf_path:
            docs = [
                item
                for item in docs
                if pdf_path == str(item.get("pdf_path") or "")
                or pdf_path in [str(path or "") for path in item.get("all_paths", [])]
            ]
        if not docs:
            return []

        safe_limit = max(1, limit_docs or self.config.query_candidate_doc_limit)
        query_tokens = set(_tokenize(question))
        query_categories = self._query_categories(question)
        ranked = semantic_rank(
            query=question,
            items=docs,
            text_getter=lambda item: str(item.get("search_text") or ""),
            embedding_getter=self._stored_embedding_for_item,
        )
        rescored: list[tuple[float, dict[str, Any]]] = []
        for index, item in enumerate(ranked):
            summary_index = dict(item.get("l0_summary_index") or {})
            score = float(len(ranked) - index)
            keyword_overlap = len(query_tokens & set(_tokenize(" ".join(summary_index.get("keywords") or []))))
            score += keyword_overlap * 0.3
            question_type_overlap = len(query_categories & set(summary_index.get("question_types") or []))
            score += question_type_overlap * 1.2
            facts = dict(item.get("fact_counts") or {})
            if "mutation" in query_categories and int(facts.get("mutations", 0) or 0) > 0:
                score += 1.0
            if "kinetics" in query_categories and int(facts.get("kinetics", 0) or 0) > 0:
                score += 1.0
            if "substrate" in query_categories and int(facts.get("substrates", 0) or 0) > 0:
                score += 1.0
            rescored.append((score, item))
        rescored.sort(key=lambda pair: pair[0], reverse=True)
        # 阈值模式：取所有 score/max_score >= threshold 的文档，上限 query_candidate_doc_max_hits
        effective_threshold = float(score_threshold if score_threshold > 0.0 else self.config.query_candidate_doc_score_threshold)
        if effective_threshold > 0.0 and rescored:
            max_score = rescored[0][0]
            max_hits = max(1, self.config.query_candidate_doc_max_hits)
            threshold_items = [
                item for score, item in rescored
                if max_score > 0 and (score / max_score) >= effective_threshold
            ]
            # 阈值命中数 >= safe_limit 时以阈值为准，否则至少保证 safe_limit 个
            if len(threshold_items) >= safe_limit:
                return threshold_items[:max_hits]
            return [item for _, item in rescored[:safe_limit]]
        return [item for _, item in rescored[:safe_limit]]

    def _candidate_passages(
        self,
        *,
        question: str,
        candidate_docs: list[dict[str, Any]],
        records: dict[str, dict[str, Any]],
        limit_passages: int = 0,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, limit_passages or self.config.query_candidate_passage_limit)
        passages: list[dict[str, Any]] = []
        for doc in candidate_docs:
            doc_id = str(doc.get("doc_id") or "")
            record = records.get(doc_id) or {}
            for passage in record.get("l1_passage_summaries", []) or []:
                if isinstance(passage, dict):
                    passages.append(dict(passage))
        if not passages:
            return []
        ranked = semantic_rank(
            query=question,
            items=passages,
            text_getter=lambda item: str(item.get("search_text") or item.get("summary_text") or ""),
            embedding_getter=self._stored_embedding_for_item,
        )
        query_categories = self._query_categories(question)
        rescored: list[tuple[float, dict[str, Any]]] = []
        for index, item in enumerate(ranked):
            score = float(len(ranked) - index)
            score += 0.8 * len(query_categories & set(item.get("question_types") or []))
            rescored.append((score, item))
        rescored.sort(key=lambda pair: pair[0], reverse=True)
        return [dict(item, score=score) for score, item in rescored[:safe_limit]]

    def _candidate_facts(
        self,
        *,
        question: str,
        candidate_docs: list[dict[str, Any]],
        records: dict[str, dict[str, Any]],
        passage_hits: list[dict[str, Any]],
        limit_facts: int,
    ) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        preferred_chunk_ids = {
            chunk_id
            for passage in passage_hits
            for chunk_id in (passage.get("source_chunk_ids") or [])
            if str(chunk_id).strip()
        }
        for doc in candidate_docs:
            doc_id = str(doc.get("doc_id") or "")
            record = records.get(doc_id) or {}
            for fact in record.get("l2_facts", []) or []:
                if isinstance(fact, dict):
                    facts.append(dict(fact))
        if not facts:
            return []
        ranked = semantic_rank(
            query=question,
            items=facts,
            text_getter=lambda item: str(item.get("search_text") or ""),
            embedding_getter=self._stored_embedding_for_item,
        )
        query_categories = self._query_categories(question)
        requested_fields = self._requested_materialized_fields(question)
        rescored: list[tuple[float, dict[str, Any]]] = []
        for index, item in enumerate(ranked):
            score = float(len(ranked) - index)
            fact_type = str(item.get("fact_type") or "")
            label = str(item.get("label") or "").lower()
            if "mutation" in query_categories and fact_type == "mutation":
                score += 1.4
            if "substrate" in query_categories and (
                fact_type == "compound" or "substrate" in label
            ):
                score += 1.2
            if "kinetics" in query_categories and (
                fact_type in {"kinetic_metric", "assay_result"} or any(
                    token in label for token in ("km", "kcat", "activity")
                )
            ):
                score += 1.2
            if preferred_chunk_ids and preferred_chunk_ids & set(item.get("anchor_ids") or []):
                score += 0.9
            rescored.append((score, item))
        rescored.sort(key=lambda pair: pair[0], reverse=True)
        safe_limit = max(1, limit_facts)
        selected: list[dict[str, Any]] = []
        seen_fact_ids: set[str] = set()

        # Guarantee category coverage for the current question so L2 results
        # don't collapse into a single dominant fact type.
        for doc in candidate_docs:
            doc_id = str(doc.get("doc_id") or "")
            if not doc_id:
                continue
            for field in requested_fields:
                for score, item in rescored:
                    fact_id = str(item.get("fact_id") or "")
                    if fact_id in seen_fact_ids or str(item.get("doc_id") or "") != doc_id:
                        continue
                    if field not in self._fact_materialized_fields(item):
                        continue
                    selected.append(dict(item, score=score))
                    if fact_id:
                        seen_fact_ids.add(fact_id)
                    break
                if len(selected) >= safe_limit:
                    return selected[:safe_limit]

        for score, item in rescored:
            fact_id = str(item.get("fact_id") or "")
            if fact_id and fact_id in seen_fact_ids:
                continue
            selected.append(dict(item, score=score))
            if fact_id:
                seen_fact_ids.add(fact_id)
            if len(selected) >= safe_limit:
                break
        return selected

    def _candidate_evidence(
        self,
        *,
        question: str,
        candidate_doc_ids: set[str],
        preferred_chunk_ids: set[str],
        limit_evidence: int,
    ) -> list[dict[str, Any]]:
        chunk_index = _read_json(self.chunk_index_path, {})
        chunks = [dict(item) for item in chunk_index.get("chunks", []) if isinstance(item, dict)]
        filtered_chunks = [
            item for item in chunks
            if not candidate_doc_ids or str(item.get("doc_id") or "") in candidate_doc_ids
        ]
        if not filtered_chunks:
            return []

        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk in filtered_chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if chunk_id and chunk_id in preferred_chunk_ids and chunk_id not in seen:
                seen.add(chunk_id)
                hits.append({**chunk, "score": 999.0})
                if len(hits) >= limit_evidence:
                    return hits

        if len(candidate_doc_ids) == 1 and FaissVectorStore.available():
            doc_id = next(iter(candidate_doc_ids))
            record = self._load_record(doc_id) or {}
            doc_index = dict(record.get("doc_chunk_index") or {})
            doc_base_raw = str(doc_index.get("doc_chunk_index_base") or "")
            doc_base = Path(doc_base_raw).expanduser() if doc_base_raw else self._doc_chunk_index_base(doc_id)
            if self._faiss_index_path(doc_base).exists():
                try:
                    query_vector = build_embedding(question, workdir=self.workdir)
                    indexed_hits = FaissVectorStore.search(
                        base_path=doc_base,
                        query_vector=query_vector,
                        limit=max(limit_evidence * 8, 24),
                    )
                except Exception:
                    indexed_hits = []
                chunk_by_id = {str(item.get("chunk_id") or ""): item for item in filtered_chunks}
                for chunk_id, score in indexed_hits:
                    chunk = chunk_by_id.get(str(chunk_id))
                    if not chunk:
                        continue
                    key = str(chunk.get("chunk_id") or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append({**chunk, "score": float(score), "vector_index_scope": "document"})
                    if len(hits) >= limit_evidence:
                        return hits

        if FaissVectorStore.available() and self.chunk_index_base.with_suffix(".faiss").exists():
            try:
                query_vector = build_embedding(question, workdir=self.workdir)
                indexed_hits = FaissVectorStore.search(
                    base_path=self.chunk_index_base,
                    query_vector=query_vector,
                    limit=max(limit_evidence * 8, 24),
                )
            except Exception:
                indexed_hits = []
            chunk_by_id = {str(item.get("chunk_id") or ""): item for item in filtered_chunks}
            for chunk_id, score in indexed_hits:
                chunk = chunk_by_id.get(str(chunk_id))
                if not chunk:
                    continue
                key = str(chunk.get("chunk_id") or "")
                if key in seen:
                    continue
                seen.add(key)
                hits.append({**chunk, "score": float(score)})
                if len(hits) >= limit_evidence:
                    return hits

        ranked = semantic_rank(
            query=question,
            items=filtered_chunks,
            text_getter=lambda item: str(item.get("search_text") or item.get("text") or ""),
            embedding_getter=self._stored_embedding_for_item,
        )
        for index, chunk in enumerate(ranked):
            key = str(chunk.get("chunk_id") or "")
            if key in seen:
                continue
            hits.append({**chunk, "score": float(len(ranked) - index)})
            if len(hits) >= limit_evidence:
                break
        return hits

    def query(
        self,
        question: str,
        *,
        doc_id: str = "",
        pdf_path: str = "",
        allowed_doc_ids: Iterable[str] | None = None,
        limit_docs: int = 0,
        limit_evidence: int = 0,
        score_threshold: float = 0.0,
        sync_evidence: bool = True,
    ) -> dict[str, Any]:
        query_text = str(question or "").strip()
        if not query_text:
            raise ValueError("question is required")

        safe_limit_docs = max(1, limit_docs or self.config.query_candidate_doc_limit)
        safe_limit_evidence = max(1, limit_evidence or self.config.query_evidence_limit)
        safe_limit_passages = max(1, self.config.query_candidate_passage_limit)

        candidate_docs = self._candidate_documents(
            question=query_text,
            doc_id=doc_id,
            pdf_path=pdf_path,
            allowed_doc_ids=allowed_doc_ids,
            limit_docs=safe_limit_docs,
            score_threshold=score_threshold,
        )
        if not candidate_docs:
            return {
                "question": query_text,
                "doc_count": 0,
                "summary_hits": [],
                "passage_hits": [],
                "fact_hits": [],
                "evidence_hits": [],
                "hits": [],
            }

        records: dict[str, dict[str, Any]] = {}
        for doc in candidate_docs:
            doc_id_key = str(doc.get("doc_id") or "")
            record = self._load_record(doc_id_key) or {}
            records[doc_id_key] = (
                self._ensure_record_evidence_synced(doc_id_key, record)
                if record and sync_evidence
                else record
            )
        passage_hits = self._candidate_passages(
            question=query_text,
            candidate_docs=candidate_docs,
            records=records,
            limit_passages=safe_limit_passages,
        )
        fact_hits = self._candidate_facts(
            question=query_text,
            candidate_docs=candidate_docs,
            records=records,
            passage_hits=passage_hits,
            limit_facts=max(safe_limit_evidence * 3, 8),
        )
        preferred_chunk_ids = {
            str(chunk_id)
            for hit in passage_hits
            for chunk_id in (hit.get("source_chunk_ids") or [])
            if str(chunk_id).strip()
        }
        evidence_hits = self._candidate_evidence(
            question=query_text,
            candidate_doc_ids={str(doc.get("doc_id") or "") for doc in candidate_docs},
            preferred_chunk_ids=preferred_chunk_ids,
            limit_evidence=safe_limit_evidence,
        )

        summary_hits = [
            {
                "doc_id": str(doc.get("doc_id") or ""),
                "title": str(doc.get("title") or ""),
                "pdf_path": str(doc.get("pdf_path") or ""),
                "summary_text": str((doc.get("l0_summary_index") or {}).get("summary_text") or ""),
                "keywords": list((doc.get("l0_summary_index") or {}).get("keywords") or []),
                "question_types": list((doc.get("l0_summary_index") or {}).get("question_types") or []),
                "page_span": list((doc.get("l0_summary_index") or {}).get("page_span") or []),
                "visual_counts": dict(doc.get("visual_counts") or {}),
                "visual_manifest_path": str((doc.get("artifacts") or {}).get("visual_manifest_path") or ""),
                "score": float(len(candidate_docs) - index),
            }
            for index, doc in enumerate(candidate_docs)
        ]

        grouped: dict[str, dict[str, Any]] = {}
        for hit in summary_hits:
            grouped[str(hit.get("doc_id") or "")] = {
                "doc_id": str(hit.get("doc_id") or ""),
                "title": str(hit.get("title") or ""),
                "pdf_path": str(hit.get("pdf_path") or ""),
                "summary_hit": hit,
                "passage_hits": [],
                "fact_hits": [],
                "evidence_hits": [],
                "structured_hits": {},
                "record_path": str(((records.get(str(hit.get("doc_id") or "")) or {}).get("artifacts") or {}).get("record_path") or ""),
                "visual_artifacts": dict((records.get(str(hit.get("doc_id") or "")) or {}).get("visual_artifacts") or {}),
                "fact_counts": dict(next((doc.get("fact_counts") for doc in candidate_docs if str(doc.get("doc_id") or "") == str(hit.get("doc_id") or "")), {}) or {}),
            }

        for hit in passage_hits:
            doc_bucket = grouped.get(str(hit.get("doc_id") or ""))
            if doc_bucket is not None:
                doc_bucket["passage_hits"].append(
                    {
                        "passage_id": str(hit.get("passage_id") or ""),
                        "summary_text": str(hit.get("summary_text") or ""),
                        "page_span": list(hit.get("page_span") or []),
                        "page_image_path": str(hit.get("page_image_path") or ""),
                        "page_image_paths": list(hit.get("page_image_paths") or []),
                        "table_image_path": str(hit.get("table_image_path") or ""),
                        "figure_image_paths": list(hit.get("figure_image_paths") or []),
                        "visual_manifest_path": str(hit.get("visual_manifest_path") or ""),
                        "section_hint": str(hit.get("section_hint") or ""),
                        "source_chunk_ids": list(hit.get("source_chunk_ids") or []),
                        "score": float(hit.get("score") or 0.0),
                    }
                )

        for hit in fact_hits:
            doc_id_key = str(hit.get("doc_id") or "")
            if not doc_id_key:
                continue
            doc_bucket = grouped.get(doc_id_key)
            if doc_bucket is None:
                continue
            doc_bucket["fact_hits"].append(
                {
                    "fact_id": str(hit.get("fact_id") or ""),
                    "evidence_id": str(hit.get("evidence_id") or ""),
                    "citation_marker": str(hit.get("citation_marker") or ""),
                    "fact_type": str(hit.get("fact_type") or ""),
                    "label": str(hit.get("label") or ""),
                    "value": str(hit.get("value") or ""),
                    "attributes": dict(hit.get("attributes") or {}),
                    "anchor_ids": list(hit.get("anchor_ids") or []),
                    "score": float(hit.get("score") or 0.0),
                }
            )

        for hit in evidence_hits:
            doc_bucket = grouped.get(str(hit.get("doc_id") or ""))
            if doc_bucket is None:
                continue
            doc_bucket["evidence_hits"].append(
                {
                    "kind": str(hit.get("kind") or ""),
                    "page": int(hit.get("page") or 0),
                    "page_span": list(hit.get("page_span") or []),
                    "chunk_id": str(hit.get("chunk_id") or ""),
                    "table_id": str(hit.get("table_id") or ""),
                    "page_image_path": str(hit.get("page_image_path") or ""),
                    "page_image_paths": list(hit.get("page_image_paths") or []),
                    "table_image_path": str(hit.get("table_image_path") or ""),
                    "figure_image_paths": list(hit.get("figure_image_paths") or []),
                    "visual_manifest_path": str(hit.get("visual_manifest_path") or ""),
                    "excerpt": str(hit.get("text") or "")[:280],
                    "score": float(hit.get("score") or 0.0),
                }
            )

        for doc_bucket in grouped.values():
            materialized: dict[str, list[dict[str, Any]]] = {
                "genes": [],
                "mutations": [],
                "substrates": [],
                "activity_metrics": [],
                "kinetics": [],
            }
            for fact in doc_bucket["fact_hits"]:
                for field in self._fact_materialized_fields(fact):
                    materialized[field].append(fact)
            doc_bucket["structured_hits"] = materialized

        hits = list(grouped.values())
        hits = [
            hit for hit in hits
            if hit["summary_hit"] or hit["passage_hits"] or hit["fact_hits"] or hit["evidence_hits"]
        ]

        return {
            "question": query_text,
            "doc_count": len(hits),
            "summary_hits": summary_hits[:safe_limit_docs],
            "passage_hits": [
                {
                    "doc_id": str(hit.get("doc_id") or ""),
                    "title": str(hit.get("title") or ""),
                    "pdf_path": str(hit.get("pdf_path") or ""),
                    "passage_id": str(hit.get("passage_id") or ""),
                    "summary_text": str(hit.get("summary_text") or ""),
                    "page_span": list(hit.get("page_span") or []),
                    "page_image_path": str(hit.get("page_image_path") or ""),
                    "page_image_paths": list(hit.get("page_image_paths") or []),
                    "table_image_path": str(hit.get("table_image_path") or ""),
                    "figure_image_paths": list(hit.get("figure_image_paths") or []),
                    "visual_manifest_path": str(hit.get("visual_manifest_path") or ""),
                    "section_hint": str(hit.get("section_hint") or ""),
                    "source_chunk_ids": list(hit.get("source_chunk_ids") or []),
                    "score": float(hit.get("score") or 0.0),
                }
                for hit in passage_hits
            ],
            "fact_hits": [
                {
                    "doc_id": str(hit.get("doc_id") or ""),
                    "fact_id": str(hit.get("fact_id") or ""),
                    "evidence_id": str(hit.get("evidence_id") or ""),
                    "citation_marker": str(hit.get("citation_marker") or ""),
                    "fact_type": str(hit.get("fact_type") or ""),
                    "label": str(hit.get("label") or ""),
                    "value": str(hit.get("value") or ""),
                    "attributes": dict(hit.get("attributes") or {}),
                    "anchor_ids": list(hit.get("anchor_ids") or []),
                    "score": float(hit.get("score") or 0.0),
                }
                for hit in fact_hits
            ],
            "evidence_hits": [
                {
                    "doc_id": str(hit.get("doc_id") or ""),
                    "title": str(hit.get("title") or ""),
                    "pdf_path": str(hit.get("pdf_path") or ""),
                    "kind": str(hit.get("kind") or ""),
                    "page": int(hit.get("page") or 0),
                    "page_span": list(hit.get("page_span") or []),
                    "chunk_id": str(hit.get("chunk_id") or ""),
                    "table_id": str(hit.get("table_id") or ""),
                    "page_image_path": str(hit.get("page_image_path") or ""),
                    "page_image_paths": list(hit.get("page_image_paths") or []),
                    "table_image_path": str(hit.get("table_image_path") or ""),
                    "figure_image_paths": list(hit.get("figure_image_paths") or []),
                    "visual_manifest_path": str(hit.get("visual_manifest_path") or ""),
                    "excerpt": str(hit.get("text") or "")[:280],
                    "score": float(hit.get("score") or 0.0),
                }
                for hit in evidence_hits
            ],
            "hits": hits,
        }
