"""版本绑定的 FAISS 缓存；只嵌入 Wiki，不读取记忆、文章或评测答案。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

from .retrieval_tools import CHANNEL_LIMIT, canonical, query_key
from .wiki_chunks import CHUNK_VERSION

TEXT_VERSION = "wiki-markdown-fragments-" + CHUNK_VERSION
QUERY_INSTRUCTION = "Retrieve enzyme Wiki evidence relevant to the described catalytic events and constraints, including supporting analogies and conflicting activities."
TRANSIENT = {"embedding_timeout", "embedding_time_budget_exceeded", "embedding_connection_failed",
             "embedding_http_408", "embedding_http_429", "embedding_http_500", "embedding_http_502",
             "embedding_http_503", "embedding_http_504"}


def faiss_modules():
    import faiss
    import numpy as np
    return faiss, np


class SemanticUnavailable(ValueError):
    """可降级的能力/索引错误，不包含远端响应或 Secret。"""


class RetrievalVersionError(RuntimeError):
    """运行中模型或知识改变必须停止，不能用降级掩盖版本混用。"""


class HybridRetriever:
    def __init__(self, api=None, config=None):
        self.api = api
        self.config = {"mode": "hybrid", "batch_size": 8, "build_batches": 4,
                       "timeout_seconds": 30, "min_similarity": 0.25,
                       "allow_runtime_build": False, **dict(config or {})}
        self.policy = hashlib.sha256(canonical({"config": self.config, "text_version": TEXT_VERSION,
                                               "query_instruction": QUERY_INSTRUCTION,
                                               "fusion_version": "identifier-first-rrf-v3"}).encode()).hexdigest()

    async def search(self, index, queries, *, limit=8, backend=None, include_pool=False):
        if (not 1 <= len(queries) <= 3 or any(not isinstance(q, str) or not q.strip() or len(q) > 800 for q in queries)
                or not 1 <= limit <= 15):
            raise ValueError("retrieval requires 1-3 non-empty queries (800 characters each), limit 1-15")
        backend = backend if backend is not None else {}
        if backend.get("policy", self.policy) != self.policy:
            raise ValueError("retrieval policy changed; initialize a fresh run")
        backend["policy"] = self.policy
        status = index.ensure()
        if backend.get("fingerprint", status["fingerprint"]) != status["fingerprint"]:
            raise RetrievalVersionError("Wiki changed; initialize a fresh run")
        backend["fingerprint"] = status["fingerprint"]
        keys = [query_key(q) for q in queries]
        recover = (backend.get("disabled_reason") in TRANSIENT and not backend.get("recovery_used")
                   and time.time() - backend.get("failed_at", time.time()) >= 30
                   and keys != backend.get("failed_queries"))
        if recover:
            backend["recovery_used"] = True
            backend.pop("disabled_reason", None)
        state = {"status": "degraded", "reason": "embedding_unavailable"}
        hits = None
        if self.config["mode"] == "lexical":
            state = {"status": "disabled", "reason": "lexical_only"}
        elif backend.get("disabled_reason"):
            state.update(reason=backend["disabled_reason"], retry_suppressed=True)
        elif self.api is None:
            state["reason"] = "embedding_capability_unavailable"
        else:
            try:
                faiss_modules()  # 未安装 FAISS 时不先花 API 费用。
                async with asyncio.timeout(self.config["timeout_seconds"]):
                    descriptor = dict(await self.api.describe())
                    signature = descriptor["signature"]
                    if backend.get("signature", signature) != signature:
                        raise RetrievalVersionError("embedding model changed; initialize a fresh run")
                    backend.update(signature=signature, descriptor=descriptor)
                    vector_status = await self._ensure_vectors(index, status, descriptor,
                                                               allow_build=self.config["allow_runtime_build"])
                    state = {**vector_status, "model": descriptor["model"],
                             "signature": signature, "model_revision": descriptor["model_revision"]}
                    if vector_status["status"] == "ready":
                        texts = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {q}" for q in queries] if "qwen3" in descriptor["model"].lower() else list(queries)
                        response = await self.api.embed(texts, expected_signature=signature, kind="query")
                        vectors = self._vectors(response, signature, len(queries))
                        hits = self._search_vectors(index, status, signature, vectors)
                if index.library.fingerprint() != status["fingerprint"]:
                    raise RetrievalVersionError("Wiki changed during retrieval; initialize a fresh run")
            except RetrievalVersionError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, ValueError) and str(exc) == "embedding_model_changed":
                    raise RetrievalVersionError("embedding model changed; initialize a fresh run") from None
                # 仅返回固定错误码，避免泄露 URL、凭据或 API 原始错误体。
                reason = str(exc) if isinstance(exc, SemanticUnavailable) else {
                    "TimeoutError": "embedding_time_budget_exceeded",
                    "ModuleNotFoundError": "faiss_unavailable", "ImportError": "faiss_unavailable",
                    "PermissionError": "embedding_permission_denied",
                    "PluginPermissionError": "embedding_permission_denied",
                }.get(type(exc).__name__, "embedding_or_index_failed")
                if isinstance(exc, ValueError) and re.fullmatch(r"embedding_[a-z0-9_]+", str(exc)):
                    reason = str(exc)
                state = {"status": "degraded", "reason": reason}
                backend["disabled_reason"] = reason
                backend.update(failed_at=time.time(), failed_queries=keys)
                state["recoverable"] = reason in TRANSIENT and not backend.get("recovery_used", False)
        if recover:
            state["recovery_attempted"] = True
        backend["last_status"] = state
        if index.library.fingerprint() != status["fingerprint"]:
            raise ValueError("Wiki changed during retrieval; initialize a fresh run")
        return index.search(queries, limit=limit, semantic_hits=hits, semantic_status=state,
                            expected_fingerprint=status["fingerprint"], include_pool=include_pool,
                            min_similarity=self.config["min_similarity"])

    @staticmethod
    def _vectors(response, signature, count):
        _faiss, np = faiss_modules()
        if response.get("signature") != signature:
            raise SemanticUnavailable("embedding_response_signature_mismatch")
        try:
            matrix = np.asarray(response["vectors"], dtype="float32")
        except (ValueError, TypeError, KeyError):
            raise SemanticUnavailable("embedding_vectors_invalid") from None
        if (matrix.ndim != 2 or matrix.shape[0] != count or not 1 <= matrix.shape[1] <= 8192
                or not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix, axis=1) == 0)):
            raise SemanticUnavailable("embedding_vectors_invalid")
        return matrix

    async def _ensure_vectors(self, index, status, descriptor, *, allow_build=True):
        cache_signature = descriptor["signature"] + ":" + TEXT_VERSION
        with closing(sqlite3.connect(index.path, timeout=2)) as db:
            ready = db.execute("SELECT fingerprint FROM vector_index WHERE signature=?", (cache_signature,)).fetchone()
        if ready == (status["fingerprint"],):
            # 在线运行只读取随知识卡发布的 faiss.index。缺失或过期时仍可
            # 使用 SQLite 中同代索引，不能为补缓存而修改生产插件目录。
            return {"status": "ready", "embedded_records": 0, "index_reused": True}
        if not allow_build:
            return {"status": "degraded", "reason": "index_not_prebuilt", "embedded_records": 0}
        with index.vector_build_guard() as acquired:
            if not acquired:
                return {"status": "degraded", "reason": "index_build_in_progress", "embedded_records": 0}
            return await self._build_vectors(index, status, descriptor)

    async def _build_vectors(self, index, status, descriptor):
        faiss, np = faiss_modules()
        signature = descriptor["signature"]
        cache_signature = signature + ":" + TEXT_VERSION
        with closing(sqlite3.connect(index.path, timeout=2)) as db:
            ready = db.execute("SELECT fingerprint FROM vector_index WHERE signature=?", (cache_signature,)).fetchone()
            if ready == (status["fingerprint"],):
                return {"status": "ready", "embedded_records": 0, "index_reused": True}
            pending = db.execute(
                "SELECT r.stable_key,r.embedding_text,r.text_sha FROM records r LEFT JOIN embeddings e "
                "ON e.stable_key=r.stable_key AND e.text_sha=r.text_sha AND e.signature=? "
                "WHERE e.stable_key IS NULL ORDER BY r.stable_key", (cache_signature,)).fetchall()
        embedded = 0
        batch_size = self.config["batch_size"]
        for offset in range(0, min(len(pending), batch_size * self.config["build_batches"]), batch_size):
            batch = pending[offset:offset + batch_size]
            if any(len(text) > 12000 for _, text, _ in batch):
                raise SemanticUnavailable("wiki_record_exceeds_embedding_budget")
            response = await self.api.embed([r[1] for r in batch], expected_signature=signature, kind="document")
            matrix = self._vectors(response, signature, len(batch))
            with closing(sqlite3.connect(index.path, timeout=2)) as db:
                db.execute("BEGIN IMMEDIATE")
                self._check_fingerprint(db, index, status)
                dims = {row[0] for row in db.execute("SELECT DISTINCT dim FROM embeddings WHERE signature=?", (cache_signature,))}
                if dims and dims != {matrix.shape[1]}:
                    raise SemanticUnavailable("embedding_dimension_changed")
                db.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?)",
                               [(key, sha, cache_signature, matrix.shape[1], matrix[i].tobytes())
                                for i, (key, _text, sha) in enumerate(batch)])
                db.commit()
            embedded += len(batch)
        remaining = len(pending) - embedded
        if remaining:
            return {"status": "degraded", "reason": "index_build_pending", "remaining_records": remaining,
                    "embedded_records": embedded, "index_reused": False,
                    "build_record_limit": batch_size * self.config["build_batches"],
                    "action": "本次有界建库额度已用完；已生成向量保存在共享缓存。使用索引维护命令补齐；本次查询按词法降级，不自动重试。"}
        # 全部当前记录齐备后才发布新代 FAISS；API 不在 SQLite 写事务内等待。
        with closing(sqlite3.connect(index.path, timeout=2)) as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_fingerprint(db, index, status)
            rows = db.execute("SELECT r.stable_key,e.dim,e.vector FROM records r JOIN embeddings e "
                              "ON e.stable_key=r.stable_key AND e.text_sha=r.text_sha AND e.signature=? "
                              "ORDER BY r.stable_key", (cache_signature,)).fetchall()
            if len(rows) != int(status["claim_count"]) or len({r[1] for r in rows}) != 1:
                raise SemanticUnavailable("embedding_index_incomplete")
            matrix = np.stack([np.frombuffer(r[2], dtype="float32") for r in rows])
            if not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix, axis=1) == 0):
                raise SemanticUnavailable("embedding_cache_invalid")
            faiss.normalize_L2(matrix)
            vector_index = faiss.IndexFlatIP(matrix.shape[1])
            vector_index.add(matrix)
            blob = faiss.serialize_index(vector_index).tobytes()
            ids = canonical([r[0] for r in rows])
            checksum = hashlib.sha256(ids.encode() + blob).hexdigest()
            db.execute("INSERT OR IGNORE INTO vector_generations SELECT * FROM vector_index WHERE signature=?", (cache_signature,))
            db.execute("INSERT OR REPLACE INTO vector_index VALUES (?,?,?,?,?,?)",
                       (status["fingerprint"], cache_signature, matrix.shape[1], ids, checksum, blob))
            db.execute("INSERT OR REPLACE INTO vector_generations VALUES (?,?,?,?,?,?)",
                       (status["fingerprint"], cache_signature, matrix.shape[1], ids, checksum, blob))
            db.commit()
        # 独立知识资产由离线管理命令发布，运行时只更新获准写入的 SQLite 缓存。
        return {"status": "ready", "embedded_records": embedded, "index_reused": False, "dimension": matrix.shape[1]}

    @staticmethod
    def _check_fingerprint(db, index, status):
        current = db.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
        if current != (status["fingerprint"],) or index.library.fingerprint() != status["fingerprint"]:
            raise RetrievalVersionError("Wiki changed during vector indexing; initialize a fresh run")

    def _search_vectors(self, index, status, signature, queries):
        faiss, np = faiss_modules()
        with closing(sqlite3.connect(index.path, timeout=2)) as db:
            self._check_fingerprint(db, index, status)
            row = db.execute("SELECT fingerprint,signature,dim,ids,checksum,data FROM vector_index WHERE signature=?",
                             (signature + ":" + TEXT_VERSION,)).fetchone()
        if not row or row[:2] != (status["fingerprint"], signature + ":" + TEXT_VERSION):
            raise SemanticUnavailable("embedding_index_version_mismatch")
        _fp, _sig, dim, raw_ids, checksum, blob = row
        if hashlib.sha256(raw_ids.encode() + blob).hexdigest() != checksum:
            raise SemanticUnavailable("faiss_checksum_mismatch")
        ids = json.loads(raw_ids)
        if dim != queries.shape[1] or len(ids) != int(status["claim_count"]) or len(ids) != len(set(ids)):
            raise SemanticUnavailable("embedding_dimension_or_ids_mismatch")
        # 运行时优先读取独立 faiss.index。文件必须与 SQLite 元数据中的
        # stable IDs、版本和序列化校验和完全匹配。
        standalone = getattr(index, "faiss_path", None)
        if standalone and standalone.is_file():
            file_blob = standalone.read_bytes()
            # 发布文件可能属于旧代次；只在校验一致时采用，否则使用上面已校验的缓存。
            if hashlib.sha256(raw_ids.encode() + file_blob).hexdigest() == checksum:
                blob = file_blob
        vector_index = faiss.deserialize_index(np.frombuffer(blob, dtype="uint8").copy())
        if vector_index.d != dim or vector_index.ntotal != len(ids):
            raise SemanticUnavailable("faiss_metadata_mismatch")
        faiss.normalize_L2(queries)
        scores, positions = vector_index.search(queries, min(CHANNEL_LIMIT, len(ids)))
        return [[(ids[int(p)], float(s)) for s, p in zip(ss, pp)
                 if p >= 0] for ss, pp in zip(scores, positions)]


def retrieval_limitations(store, run_id):
    reasons = set()
    for stage in ("mechanism", "screen"):
        audit = store._read_json_if_exists(store._run_path(run_id, f"retrieval_{stage}.json")) or {}
        for row in audit.get("rounds", []):
            semantic = row.get("semantic", {})
            if semantic and semantic.get("status") != "ready":
                reasons.add(semantic.get("reason", "embedding_unavailable"))
    if not reasons:
        return []
    labels = {"embedding_timeout": "模型请求超时", "embedding_time_budget_exceeded": "向量处理超出时间预算",
              "embedding_capability_unavailable": "宿主未提供向量能力", "embedding_disabled": "模型配置关闭",
              "embedding_permission_denied": "向量能力未授权", "index_build_pending": "向量索引尚未建完",
              "index_build_in_progress": "其他会话正在构建共享索引",
              "faiss_unavailable": "FAISS 不可用", "lexical_only": "配置选择纯词法检索",
              "faiss_checksum_mismatch": "向量索引校验失败"}
    labels["index_not_prebuilt"] = "向量索引未预建，需管理员更新；聊天不会自动建库"
    explanation = "、".join(dict.fromkeys(labels.get(r, "向量服务或索引未通过校验") for r in sorted(reasons)))
    backend = store._read_json_if_exists(store._run_path(run_id, "retrieval_backend.json")) or {}
    recovered = backend.get("last_status", {}).get("status") == "ready"
    if recovered:
        return [f"检索覆盖限制：早期部分查询仅使用词面匹配（{explanation}）；后续向量检索已恢复。早期候选池未自动重算，仍可能漏掉同义或跨语言线索；不能将未召回作为反证。"]
    return [f"检索覆盖限制：部分查询仅使用词面匹配（{explanation}），可能漏掉同义或跨语言的相关反应；不能将未召回作为反证。"]
