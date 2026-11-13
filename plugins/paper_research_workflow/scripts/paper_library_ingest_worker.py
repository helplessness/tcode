#!/usr/bin/env python3
"""Sandbox worker for paper library PDF ingestion.

The web/plugin process is control-plane only. This script runs inside the
sandbox container, writes artifacts into the mounted workspace, and rewrites
stored /workspace paths back to host workspace paths before returning.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
import types
from pathlib import Path
from typing import Any, Callable, Iterable


TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
EMBEDDING_DIM = 256


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(str(text or ""))]


def _bigram_tokens(tokens: list[str]) -> list[str]:
    result = list(tokens)
    for index in range(len(tokens) - 1):
        result.append(f"{tokens[index]}_{tokens[index + 1]}")
    for index in range(len(tokens) - 2):
        result.append(f"{tokens[index]}_{tokens[index + 1]}_{tokens[index + 2]}")
    return result


def _hashed_embedding(text: str, dim: int = EMBEDDING_DIM, workdir: str | None = None) -> list[float]:
    vector = [0.0] * dim
    tokens = _tokenize(text)
    if not tokens:
        return vector
    for token in _bigram_tokens(tokens):
        digest = hashlib.sha256(token.encode("utf-8", errors="ignore")).digest()
        slot = int.from_bytes(digest[:8], "big") % dim
        vector[slot] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    return sum(float(left[index]) * float(right[index]) for index in range(size))


def _semantic_rank(
    *,
    query: str,
    items: Iterable[Any],
    text_getter: Callable[[Any], str],
    embedding_getter: Callable[[Any], list[float] | None],
) -> list[Any]:
    query_text = str(query or "").strip()
    materialized = list(items)
    if not query_text:
        return sorted(materialized, key=lambda _: 0, reverse=True)
    query_tokens = set(_tokenize(query_text))
    query_embedding = _hashed_embedding(query_text)
    scored: list[tuple[float, Any]] = []
    for item in materialized:
        text = text_getter(item) or ""
        embedding = embedding_getter(item) or []
        token_overlap = 0.0
        if query_tokens:
            item_tokens = set(_tokenize(text))
            token_overlap = len(query_tokens & item_tokens) / max(1, len(query_tokens))
        similarity = _cosine_similarity(query_embedding, embedding)
        scored.append((similarity * 0.75 + token_overlap * 0.25, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


class _FaissVectorStore:
    @classmethod
    def _index_path(cls, base_path: Path) -> Path:
        return base_path.with_suffix(base_path.suffix + ".faiss")

    @classmethod
    def _meta_path(cls, base_path: Path) -> Path:
        return base_path.with_suffix(base_path.suffix + ".faiss.meta.json")

    @classmethod
    def available(cls) -> bool:
        try:
            import faiss  # type: ignore  # noqa: F401
            import numpy  # type: ignore  # noqa: F401
            return True
        except Exception:
            return False

    @classmethod
    def write(cls, *, base_path: Path, ids: list[str], vectors: list[list[float]]) -> None:
        if not vectors:
            return
        import faiss  # type: ignore
        import numpy as np  # type: ignore

        dim = len(vectors[0])
        index = faiss.IndexFlatIP(dim)
        array = np.array(vectors, dtype="float32")
        faiss.normalize_L2(array)
        index.add(array)
        cls._index_path(base_path).parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(cls._index_path(base_path)))
        cls._meta_path(base_path).write_text(
            json.dumps({"ids": ids, "dim": dim}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def search(cls, *, base_path: Path, query_vector: list[float], limit: int) -> list[tuple[str, float]]:
        index_path = cls._index_path(base_path)
        meta_path = cls._meta_path(base_path)
        if not index_path.exists() or not meta_path.exists():
            return []
        import faiss  # type: ignore
        import numpy as np  # type: ignore

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ids = meta.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return []
        index = faiss.read_index(str(index_path))
        query = np.array([query_vector], dtype="float32")
        faiss.normalize_L2(query)
        scores, positions = index.search(query, max(1, limit))
        results: list[tuple[str, float]] = []
        for score, position in zip(scores[0], positions[0]):
            if int(position) < 0 or int(position) >= len(ids):
                continue
            results.append((str(ids[int(position)]), float(score)))
        return results


def _install_minimal_tcode_memory_modules() -> None:
    """仅在隔离 Worker 缺少主项目模块时安装最小内存检索替身。

    单元测试可能在完整服务进程中直接调用 Worker；此时必须复用已经加载的
    正式模块，不能用轻量替身覆盖全局 ``sys.modules`` 并污染后续 Wiki 测试。
    """

    existing_retriever = sys.modules.get("tcode_agent.memory.retriever")
    existing_vector_store = sys.modules.get("tcode_agent.memory.vector_store")
    if (
        existing_retriever is not None
        and hasattr(existing_retriever, "build_embedding")
        and hasattr(existing_retriever, "semantic_rank")
        and existing_vector_store is not None
        and hasattr(existing_vector_store, "FaissVectorStore")
    ):
        return
    tcode_agent = sys.modules.setdefault("tcode_agent", types.ModuleType("tcode_agent"))
    memory = sys.modules.setdefault("tcode_agent.memory", types.ModuleType("tcode_agent.memory"))
    retriever = types.ModuleType("tcode_agent.memory.retriever")
    retriever.build_embedding = _hashed_embedding
    retriever.semantic_rank = _semantic_rank
    vector_store = types.ModuleType("tcode_agent.memory.vector_store")
    vector_store.FaissVectorStore = _FaissVectorStore
    setattr(tcode_agent, "memory", memory)
    setattr(memory, "retriever", retriever)
    setattr(memory, "vector_store", vector_store)
    sys.modules["tcode_agent.memory.retriever"] = retriever
    sys.modules["tcode_agent.memory.vector_store"] = vector_store


def _load_library_module() -> Any:
    library_path = Path(__file__).resolve().parents[1] / "library.py"
    spec = importlib.util.spec_from_file_location("paper_research_workflow_sandbox_library", library_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load paper library module: {library_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _rewrite_string(value: str, rewrites: list[tuple[str, str]]) -> str:
    result = value
    for source, target in rewrites:
        if result == source:
            result = target
        elif result.startswith(f"{source.rstrip('/')}/"):
            result = f"{target.rstrip('/')}/{result[len(source.rstrip('/')) + 1:]}"
    return result


def _rewrite_paths(value: Any, rewrites: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        return _rewrite_string(value, rewrites)
    if isinstance(value, list):
        return [_rewrite_paths(item, rewrites) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_paths(item, rewrites) for key, item in value.items()}
    return value


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("worker input must be a JSON object")
    return payload


def run(input_path: Path, output_path: Path) -> int:
    payload = _read_json(input_path)
    _install_minimal_tcode_memory_modules()
    library = _load_library_module()

    rewrites: list[tuple[str, str]] = []
    for item in list(payload.get("path_rewrites") or []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or "").strip().rstrip("/")
        target = str(item.get("to") or "").strip().rstrip("/")
        if source and target and source != target:
            rewrites.append((source, target))

    config_payload = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    config = library.PaperLibraryConfig(**config_payload)
    store = library.PaperLibraryStore(
        library_root=Path(str(payload.get("library_root") or "")).expanduser().resolve(),
        workdir=str(payload.get("embedding_workdir") or "/tmp/paper_library_hash_embeddings"),
        config=config,
        path_rewrites=rewrites,
    )
    text_payload = payload.get("text_payload") if isinstance(payload.get("text_payload"), dict) else None
    table_payload = payload.get("table_payload") if isinstance(payload.get("table_payload"), dict) else None
    result = store.ingest_pdf(
        str(payload.get("pdf_path") or ""),
        task_id=str(payload.get("task_id") or ""),
        title=str(payload.get("title") or ""),
        pmid=str(payload.get("pmid") or ""),
        doi=str(payload.get("doi") or ""),
        source_url=str(payload.get("source_url") or ""),
        text_payload_override=text_payload,
        table_payload_override=table_payload,
        force_reindex=bool(payload.get("force_reindex", False)),
        ingest_attempt_id=str(payload.get("ingest_attempt_id") or ""),
    )

    if rewrites:
        result = _rewrite_paths(result, rewrites)

    output = {
        "ok": True,
        "result": result,
        "sandbox_worker": True,
        "faiss_available": _FaissVectorStore.available(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sandbox paper library ingest worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        return run(Path(args.input), Path(args.output))
    except Exception as exc:
        error_payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(error_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(error_payload["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
