"""小规模 Skill 向量索引。

当前 Skill 数量较少，使用可审计的 JSON 索引即可；后续规模增长后可替换为
FAISS，而不改变路由器接口。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from ..config.manager import ConfigManager
from ..memory.embedding_provider import EmbeddingProvider
from .routing_config import resolve_skill_routing_output_path
from .routing_profile import SkillRoutingProfile


_TOKEN_RE = re.compile(r"[a-z0-9_+.-]{2,}|[\u4e00-\u9fff]+", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    result: list[str] = []
    for match in _TOKEN_RE.finditer(str(text or "").lower()):
        token = match.group(0)
        if token and "\u4e00" <= token[0] <= "\u9fff":
            result.append(token)
            result.extend(token[index : index + 2] for index in range(max(0, len(token) - 1)))
        else:
            result.append(token)
    return result


def stable_local_embedding(text: str, dimensions: int = 256) -> list[float]:
    """使用稳定哈希生成离线向量，保证 embedding 服务不可用时仍可路由。"""

    dimensions = max(32, min(int(dimensions or 256), 4096))
    vector = [0.0] * dimensions
    tokens = _tokens(text)
    if not tokens:
        return vector
    expanded = list(tokens)
    expanded.extend(f"{tokens[index]}_{tokens[index + 1]}" for index in range(len(tokens) - 1))
    for token in expanded:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[slot] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left)) or 1.0
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right)) or 1.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


class SkillVectorIndex:
    _lock = threading.Lock()
    _memory: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _provider_signature(workdir: Optional[str]) -> str:
        ConfigManager.load(workdir)
        cfg = ConfigManager.get_embeddings()
        options = cfg.get("options", {}) if isinstance(cfg.get("options"), dict) else {}
        payload = {
            "enabled": bool(cfg.get("enabled", False)),
            "provider": str(cfg.get("provider") or "ollama"),
            "model": str(cfg.get("model") or "nomic-embed-text"),
            "base_url": str(options.get("baseURL") or options.get("base_url") or ""),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

    @staticmethod
    def _document_hash(text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    @classmethod
    def _load_cache(cls, path: Path) -> dict[str, Any]:
        key = str(path)
        cached = cls._memory.get(key)
        if cached is not None:
            return cached
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        cls._memory[key] = payload
        return payload

    @classmethod
    def _save_cache(cls, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        cls._memory[str(path)] = payload

    @classmethod
    def build(
        cls,
        profiles: list[SkillRoutingProfile],
        *,
        workdir: Optional[str],
        config: dict[str, Any],
    ) -> tuple[dict[str, list[list[float]]], dict[str, Any]]:
        vector_cfg = config.get("vector", {}) if isinstance(config.get("vector"), dict) else {}
        if not bool(vector_cfg.get("enabled", True)):
            return {}, {"enabled": False, "backend": "disabled"}
        dimensions = max(32, int(vector_cfg.get("dimensions", 256) or 256))
        path = resolve_skill_routing_output_path(
            str(vector_cfg.get("index_path") or ".tcode/skill-routing/vector-index.json"),
            workdir=workdir,
        )
        provider_signature = cls._provider_signature(workdir)
        external_enabled = EmbeddingProvider.is_enabled(workdir)

        documents: list[tuple[str, str, str]] = []
        for profile in profiles:
            skill_key = profile.skill.skill_id or profile.skill.name
            for text in profile.vector_documents():
                documents.append((skill_key, cls._document_hash(text), text))

        with cls._lock:
            cached = cls._load_cache(path)
            cached_backend = str(cached.get("backend") or "")
            cached_signature = str(cached.get("provider_signature") or "")
            entries = cached.get("entries") if isinstance(cached.get("entries"), dict) else {}

            use_external_cache = external_enabled and cached_backend == "external" and cached_signature == provider_signature
            use_local_cache = not external_enabled and cached_backend == "local-v1"
            if not (use_external_cache or use_local_cache):
                entries = {}

            missing = [(doc_hash, text) for _, doc_hash, text in documents if doc_hash not in entries]
            backend = "external" if external_enabled else "local-v1"
            if missing:
                texts = [text for _, text in missing]
                if external_enabled:
                    try:
                        vectors = EmbeddingProvider.embed_many(texts, workdir)
                    except Exception:
                        backend = "local-v1"
                        entries = {}
                        vectors = [stable_local_embedding(text, dimensions) for _, _, text in documents]
                        missing = [(doc_hash, text) for _, doc_hash, text in documents]
                else:
                    vectors = [stable_local_embedding(text, dimensions) for text in texts]
                for (doc_hash, _), vector in zip(missing, vectors):
                    entries[doc_hash] = vector

            payload = {
                "version": 1,
                "backend": backend,
                "provider_signature": provider_signature if backend == "external" else "local-v1",
                "entries": entries,
            }
            try:
                cls._save_cache(path, payload)
            except Exception:
                cls._memory[str(path)] = payload

        skill_vectors: dict[str, list[list[float]]] = {}
        for skill_key, doc_hash, _ in documents:
            vector = entries.get(doc_hash)
            if isinstance(vector, list):
                skill_vectors.setdefault(skill_key, []).append(vector)
        return skill_vectors, {
            "enabled": True,
            "backend": backend,
            "provider_signature": payload["provider_signature"],
            "index_path": str(path),
            "document_count": len(documents),
        }

    @classmethod
    def embed_query(
        cls,
        query: str,
        *,
        workdir: Optional[str],
        config: dict[str, Any],
        metadata: dict[str, Any],
    ) -> list[float]:
        backend = str(metadata.get("backend") or "")
        vector_cfg = config.get("vector", {}) if isinstance(config.get("vector"), dict) else {}
        dimensions = max(32, int(vector_cfg.get("dimensions", 256) or 256))
        if backend == "external":
            try:
                return EmbeddingProvider.embed(query, workdir)
            except Exception:
                return []
        if backend == "local-v1":
            return stable_local_embedding(query, dimensions)
        return []

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._memory = {}
