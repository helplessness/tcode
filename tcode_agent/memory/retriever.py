"""Semantic retrieval helpers.

Uses a configurable embedding backend when enabled, and falls back to a
deterministic hashed bag-of-words embedding otherwise.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Callable, Iterable, Optional

from .embedding_provider import EmbeddingProvider


TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
EMBEDDING_DIM = 256
HASH_EMBEDDING_VERSION = f"hash-blake2b-v2:{EMBEDDING_DIM}"


def _tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(str(text or ""))]


def build_search_text(*, content: str, tags: list[str], fields: dict[str, Any]) -> str:
    parts = [str(content or "").strip()]
    parts.extend(str(tag).strip() for tag in (tags or []) if str(tag).strip())
    for key, value in (fields or {}).items():
        if isinstance(value, list):
            if value:
                parts.append(f"{key}: {' '.join(str(v) for v in value)}")
        elif value not in {"", None, 0.0}:
            parts.append(f"{key}: {value}")
    return "\n".join(part for part in parts if part)


def _bigram_tokens(tokens: list[str]) -> list[str]:
    """Build unigram + bigram + trigram tokens for better semantic discrimination.

    Single tokens like 'protein' and 'engineering' are weak signals alone,
    but the compound 'protein_engineering' captures a specific concept.
    Unigrams are included to preserve single-keyword matching.
    """
    result = list(tokens)
    for i in range(len(tokens) - 1):
        result.append(f"{tokens[i]}_{tokens[i + 1]}")
    for i in range(len(tokens) - 2):
        result.append(f"{tokens[i]}_{tokens[i + 1]}_{tokens[i + 2]}")
    return result

def _hashed_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    vec = [0.0] * dim
    tokens = _tokenize(text)
    if not tokens:
        return vec

    for token in _bigram_tokens(tokens):
        # Python's built-in hash is salted per process, so persisted vectors made
        # with it cannot be queried reliably after a server restart.  BLAKE2 is
        # stable across processes and platforms while remaining inexpensive.
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest, "big") % dim
        vec[slot] += 1.0

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _hashed_embedding_version(dim: int) -> str:
    return HASH_EMBEDDING_VERSION if dim == EMBEDDING_DIM else f"hash-blake2b-v2:{dim}"


def build_embedding_with_version(
    text: str,
    dim: int = EMBEDDING_DIM,
    workdir: Optional[str] = None,
) -> tuple[list[float], str]:
    if EmbeddingProvider.is_enabled(workdir):
        try:
            return EmbeddingProvider.embed(text, workdir), EmbeddingProvider.version(workdir)
        except Exception:
            pass
    return _hashed_embedding(text, dim=dim), _hashed_embedding_version(dim)


def build_embeddings_with_version(
    texts: Iterable[str],
    *,
    dim: int = EMBEDDING_DIM,
    workdir: Optional[str] = None,
) -> tuple[list[list[float]], str]:
    """Build a batch of compatible vectors and return their version identifier."""

    materialized = [str(text or "") for text in texts]
    if not materialized:
        return [], _hashed_embedding_version(dim)
    if EmbeddingProvider.is_enabled(workdir):
        try:
            return (
                EmbeddingProvider.embed_many(materialized, workdir),
                EmbeddingProvider.version(workdir),
            )
        except Exception:
            pass
    return (
        [_hashed_embedding(text, dim=dim) for text in materialized],
        _hashed_embedding_version(dim),
    )


def build_embedding(text: str, dim: int = EMBEDDING_DIM, workdir: Optional[str] = None) -> list[float]:
    return build_embedding_with_version(text, dim=dim, workdir=workdir)[0]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    return sum(float(left[i]) * float(right[i]) for i in range(size))


def semantic_rank(
    *,
    query: str,
    items: Iterable[Any],
    text_getter: Callable[[Any], str],
    embedding_getter: Callable[[Any], list[float] | None],
    embedding_version_getter: Callable[[Any], str | None] | None = None,
    workdir: Optional[str] = None,
) -> list[Any]:
    query_text = str(query or "").strip()
    materialized = list(items)
    if not query_text:
        return sorted(materialized, key=lambda _: 0, reverse=True)

    query_tokens = set(_tokenize(query_text))
    query_embedding, query_embedding_version = build_embedding_with_version(
        query_text,
        workdir=workdir,
    )
    missing_items: list[Any] = []
    missing_texts: list[str] = []
    for item in materialized:
        if embedding_getter(item):
            continue
        missing_items.append(item)
        missing_texts.append(str(text_getter(item) or ""))
    generated_by_identity: dict[int, list[float]] = {}
    generated_version = ""
    if missing_items:
        generated_vectors, generated_version = build_embeddings_with_version(
            missing_texts,
            workdir=workdir,
        )
        generated_by_identity = {
            id(item): vector
            for item, vector in zip(missing_items, generated_vectors)
        }
    scored: list[tuple[float, Any]] = []
    for item in materialized:
        text = text_getter(item) or ""
        embedding = embedding_getter(item) or generated_by_identity.get(id(item), [])
        token_overlap = 0.0
        if query_tokens:
            item_tokens = set(_tokenize(text))
            token_overlap = len(query_tokens & item_tokens) / max(1, len(query_tokens))
        compatible = True
        if embedding_version_getter is not None:
            item_version = str(embedding_version_getter(item) or "").strip()
            # Legacy vectors have no trustworthy version.  Falling back to the
            # lexical component is safer than mixing vectors from different
            # models or from the old process-randomized hash implementation.
            compatible = bool(item_version and item_version == query_embedding_version)
        elif id(item) in generated_by_identity:
            compatible = bool(
                generated_version and generated_version == query_embedding_version
            )
        similarity = cosine_similarity(query_embedding, embedding) if compatible else 0.0
        score = similarity * 0.75 + token_overlap * 0.25
        scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]
