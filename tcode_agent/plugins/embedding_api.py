"""仅调用宿主配置的 embedding 模型；不是通用内网 HTTP 出口。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from urllib.parse import urlsplit

import aiohttp

from ..config.manager import ConfigManager
from ..config.options import resolve_option_value
from ..memory.embedding_provider import EmbeddingProvider
from .permissions import check_permission, PluginPermissionError


class PluginEmbeddingAPI:
    """固定服务地址/模型，插件只能提交有界文本，不能指定 URL 或读取密钥。

    这是显式 model.embedding 授权的模型 API，与记忆系统的哈希降级开关无关。
    不回退模型、不重试、不跟随重定向、不返回 API 原始错误或凭据。
    """

    def __init__(self, workdir, permissions, ensure_active=lambda: None):
        self._workdir, self._permissions = workdir, list(permissions)
        self._ensure_active = ensure_active
        ConfigManager.load(workdir)
        self._config = copy.deepcopy(EmbeddingProvider.get_config(workdir))
        self._descriptor = None

    def _settings(self):
        self._ensure_active()
        if not check_permission(self._permissions, "model.embedding", "configured", workdir=self._workdir):
            raise PluginPermissionError("embedding_permission_denied")
        cfg = self._config
        if not cfg.get("enabled"):
            raise ValueError("embedding_disabled")
        provider = str(cfg.get("provider") or "")
        if provider not in {"ollama", "openai_compatible"}:
            raise ValueError("embedding_provider_unsupported")
        options = cfg.get("options") or {}
        base = resolve_option_value(options, "baseURL", "base_url").rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("embedding_endpoint_invalid")
        model = str(cfg.get("model") or "").strip()
        if not model:
            raise ValueError("embedding_model_missing")
        if provider == "ollama" and base.endswith("/v1"):
            base = base[:-3]
        timeout = max(1, min(30, int(options.get("timeoutSeconds", 30))))
        key = resolve_option_value(options, "apiKey", "api_key")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return provider, base, model, options, timeout, headers

    async def _request(self, method, url, payload, timeout, headers):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False) as client:
                async with client.request(method, url, json=payload, headers=headers, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f"embedding_http_{response.status}")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ValueError("embedding_response_too_large")
                    return json.loads(raw)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise ValueError("embedding_timeout") from None
        except aiohttp.ClientError:
            raise ValueError("embedding_connection_failed") from None
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("embedding_response_invalid_json") from None

    async def describe(self):
        provider, base, model, options, timeout, headers = self._settings()
        revision = str(options.get("modelRevision") or "")
        if provider == "ollama":
            # 固定模型 API 的版本元数据，不是任意 URL 检索。
            tags = await self._request("GET", base + "/api/tags", None, timeout, headers)
            matches = [m for m in tags.get("models", []) if m.get("name") == model or m.get("model") == model]
            if len(matches) != 1 or not matches[0].get("digest"):
                raise ValueError("embedding_model_revision_unavailable")
            revision = str(matches[0]["digest"])
        elif not revision:
            raise ValueError("embedding_model_revision_required")
        descriptor = {"provider": provider, "model": model, "model_revision": revision,
                      "endpoint_hash": hashlib.sha256(base.encode()).hexdigest(),
                      "dimensions": options.get("dimensions"), "protocol": "embedding-api-v1"}
        descriptor["signature"] = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
        self._descriptor = descriptor
        return dict(descriptor)

    async def embed(self, texts, *, expected_signature, kind="document"):
        provider, base, model, options, timeout, headers = self._settings()
        if kind not in {"document", "query"}:
            raise ValueError("embedding_kind_invalid")
        if (not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= 16
                or any(not isinstance(t, str) or not t.strip() or len(t) > 12000 for t in texts)
                or sum(len(t.encode()) for t in texts) > 196608):
            raise ValueError("embedding_input_budget_exceeded")
        if not self._descriptor or self._descriptor["signature"] != expected_signature:
            raise ValueError("embedding_signature_mismatch")
        payload = {"model": model, "input": list(texts)}
        if provider == "ollama":
            payload["truncate"] = False
            path = "/api/embed"
        else:
            payload["encoding_format"] = "float"
            if options.get("dimensions"):
                payload["dimensions"] = options["dimensions"]
            path = "/embeddings"
        data = await self._request("POST", base + path, payload, timeout, headers)
        if data.get("model", model) != model:
            raise ValueError("embedding_response_model_mismatch")
        if provider == "ollama":
            vectors = data.get("embeddings")
        else:
            entries = data.get("data", [])
            if sorted(e.get("index", -1) for e in entries) != list(range(len(texts))):
                raise ValueError("embedding_response_indices_invalid")
            vectors = [e.get("embedding") for e in sorted(entries, key=lambda e: e["index"])]
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("embedding_response_count_mismatch")
        dims = set()
        for vector in vectors:
            if not isinstance(vector, list) or not 1 <= len(vector) <= 8192:
                raise ValueError("embedding_vector_invalid")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                raise ValueError("embedding_vector_nonfinite")
            if sum(v * v for v in vector) <= 0:
                raise ValueError("embedding_vector_zero")
            dims.add(len(vector))
        if len(dims) != 1 or (options.get("dimensions") and dims != {options["dimensions"]}):
            raise ValueError("embedding_dimension_mismatch")
        if (await self.describe())["signature"] != expected_signature:
            raise ValueError("embedding_model_changed")
        return {"vectors": vectors, "signature": expected_signature, "dimension": next(iter(dims))}
