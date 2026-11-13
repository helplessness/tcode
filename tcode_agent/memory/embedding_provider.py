"""Embedding provider backed by configurable models, with Ollama support."""

from __future__ import annotations

import json
import hashlib
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ..config.manager import ConfigManager
from ..config.options import resolve_option_value


def _truthy_env(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_base_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return "http://localhost:11434"
    if value.endswith("/v1"):
        return value[:-3]
    return value


def _post_json(url: str, payload: dict[str, Any], timeout: int = 30, headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class EmbeddingProvider:
    _unavailable_until: dict[str, float] = {}

    @classmethod
    def strategy(cls) -> str:
        value = os.environ.get("TCODE_EMBEDDING_STRATEGY", "").strip().lower()
        if value:
            # 无效配置安全回退，不意外发送文本到外部服务。
            return value if value in {"model_first", "local_only"} else "local_only"
        return "local_only" if _truthy_env("TCODE_LOCAL_EMBEDDING_ENABLED") else "configured"

    @classmethod
    def minimum_similarity(cls) -> float:
        try:
            value = float(os.environ.get("TCODE_EMBEDDING_MIN_SIMILARITY", "0.35" if cls.strategy() == "model_first" else "-1"))
            return max(-1.0, min(1.0, value)) if math.isfinite(value) else 0.35
        except ValueError:
            return 0.35

    @classmethod
    def local_embedding_enabled(cls) -> bool:
        """Return whether all embedding calls must stay in the local process."""

        return cls.strategy() == "local_only"

    @classmethod
    def _provider_key(cls, cfg: dict[str, Any]) -> str:
        options = cfg.get("options", {}) if isinstance(cfg.get("options"), dict) else {}
        base_url = _normalize_base_url(resolve_option_value(options, "baseURL", "base_url"))
        model = str(cfg.get("model") or "nomic-embed-text").strip() or "nomic-embed-text"
        return f"{base_url}|{model}"

    @classmethod
    def version(cls, workdir: Optional[str] = None) -> str:
        """Return a non-secret identifier for vectors produced by this provider."""

        cfg = cls.get_config(workdir)
        provider = str(cfg.get("provider", "ollama") or "ollama").strip().lower()
        options = cfg.get("options", {}) if isinstance(cfg.get("options"), dict) else {}
        base_url = _normalize_base_url(resolve_option_value(options, "baseURL", "base_url"))
        model = str(cfg.get("model") or "nomic-embed-text").strip() or "nomic-embed-text"
        endpoint_hash = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:10]
        revision = str(cfg.get("revision") or "").strip()
        revision_suffix = f":{hashlib.sha256(revision.encode()).hexdigest()[:10]}" if revision else ""
        return f"{provider}:{model}:{endpoint_hash}{revision_suffix}"

    @classmethod
    def _is_temporarily_unavailable(cls, key: str) -> bool:
        until = float(cls._unavailable_until.get(key, 0.0) or 0.0)
        now = time.time()
        if until <= now:
            if key in cls._unavailable_until:
                cls._unavailable_until.pop(key, None)
            return False
        return True

    @classmethod
    def _mark_unavailable(cls, key: str) -> None:
        try:
            cooldown_seconds = float(
                os.environ.get("TCODE_EMBEDDING_UNAVAILABLE_COOLDOWN_SECONDS", "120")
            )
        except Exception:
            cooldown_seconds = 120.0
        cls._unavailable_until[key] = time.time() + max(5.0, min(cooldown_seconds, 1800.0))

    @classmethod
    def get_config(cls, workdir: Optional[str] = None) -> dict[str, Any]:
        ConfigManager.load(workdir)
        cfg = dict(ConfigManager.get_embeddings())
        if cls.strategy() == "model_first":
            cfg["enabled"] = True
        # A model explicitly chosen in desktop settings wins over inherited
        # endpoint/model defaults. local_only remains enforced by is_enabled.
        if os.environ.get('TCODE_DESKTOP_MANAGED') == '1' and ConfigManager.get_config_copy().get('desktop_embedding_default'):
            return cfg
        for field in ("provider", "model", "revision"):
            value = os.environ.get(f"TCODE_EMBEDDING_{field.upper()}", "").strip()
            if value:
                cfg[field] = value
        base_url = os.environ.get("TCODE_EMBEDDING_BASE_URL", "").strip()
        if base_url:
            options = dict(cfg.get("options") or {})
            # 环境文件显式端点优先于 tcode.json 中的间接环境引用。
            for key in ("baseURL", "base_url", "baseURLEnv", "baseURL_env", "base_urlEnv", "base_url_env"):
                options.pop(key, None)
            options["baseURL"] = base_url
            cfg["options"] = options
        return cfg

    @staticmethod
    def _parse_vector(value: Any) -> list[float]:
        if not isinstance(value, list) or not value:
            raise ValueError("Embedding response contains no vector")
        if any(isinstance(item, bool) or not isinstance(item, (float, int)) for item in value):
            raise ValueError("Embedding response contains non-numeric values")
        vector = [float(item) for item in value]
        if not all(math.isfinite(item) for item in vector) or not any(vector):
            raise ValueError("Embedding response contains invalid vector values")
        return vector

    @classmethod
    def is_enabled(cls, workdir: Optional[str] = None) -> bool:
        # ``is_enabled`` means the external embedding provider is available.
        # Local deterministic embeddings are handled by the callers' existing
        # fallback paths and must never reach Ollama when this switch is on.
        if cls.local_embedding_enabled():
            return False
        cfg = cls.get_config(workdir)
        return bool(cfg.get("enabled", False))

    @classmethod
    def embed(cls, text: str, workdir: Optional[str] = None) -> list[float]:
        cfg = cls.get_config(workdir)
        provider = str(cfg.get("provider", "ollama") or "ollama").strip().lower()
        if provider == 'openai_compatible':
            return cls._embed_openai([text], cfg)[0]
        if provider != "ollama":
            raise RuntimeError(f"Unsupported embedding provider: {provider}")
        provider_key = cls._provider_key(cfg)
        if cls._is_temporarily_unavailable(provider_key):
            raise RuntimeError("Embedding provider temporarily unavailable")
        return cls._embed_ollama(text, cfg)

    @classmethod
    def _embed_openai(cls, texts, cfg):
        options = cfg.get('options') or {}
        base = resolve_option_value(options, 'baseURL', 'base_url').rstrip('/')
        key = resolve_option_value(options, 'apiKey', 'api_key')
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'
        body = {'model': cfg['model'], 'input': texts, 'encoding_format': 'float'}
        if options.get('dimensions'):
            body['dimensions'] = options['dimensions']
        req = urllib.request.Request(base + '/embeddings', data=json.dumps(body).encode(), headers=headers, method='POST')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError()
                entries = json.loads(raw).get('data', [])
            if sorted(e.get('index', -1) for e in entries) != list(range(len(texts))):
                raise ValueError()
            vectors = [cls._parse_vector(e.get('embedding')) for e in sorted(entries, key=lambda e: e['index'])]
            if len({len(v) for v in vectors}) != 1 or any(len(v) > 8192 for v in vectors):
                raise ValueError()
            return vectors
        except Exception:
            raise RuntimeError('嵌入模型调用失败，请检查连接、密钥与返回格式') from None

    @classmethod
    def embed_many(
        cls,
        texts: list[str],
        workdir: Optional[str] = None,
    ) -> list[list[float]]:
        """批量生成向量，优先使用 Ollama 的批量 `/api/embed` 接口。"""

        materialized = [str(text or "") for text in texts]
        if not materialized:
            return []
        cfg = cls.get_config(workdir)
        provider = str(cfg.get("provider", "ollama") or "ollama").strip().lower()
        if provider == 'openai_compatible':
            return cls._embed_openai(materialized, cfg)
        if provider != "ollama":
            raise RuntimeError(f"Unsupported embedding provider: {provider}")
        provider_key = cls._provider_key(cfg)
        if cls._is_temporarily_unavailable(provider_key):
            raise RuntimeError("Embedding provider temporarily unavailable")

        options = cfg.get("options", {}) if isinstance(cfg.get("options"), dict) else {}
        base_url = _normalize_base_url(resolve_option_value(options, "baseURL", "base_url"))
        model = str(cfg.get("model") or "nomic-embed-text").strip() or "nomic-embed-text"
        try:
            timeout_seconds = int(os.environ.get("TCODE_EMBEDDING_HTTP_TIMEOUT_SECONDS", "5"))
        except Exception:
            timeout_seconds = 5
        timeout_seconds = max(2, min(timeout_seconds, 30))

        try:
            data = _post_json(
                f"{base_url}/api/embed",
                {"model": model, "input": materialized},
                timeout=timeout_seconds,
                headers=cls._embedding_headers(options),
            )
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and len(embeddings) == len(materialized):
                parsed = [cls._parse_vector(vector) for vector in embeddings]
                if len({len(vector) for vector in parsed}) == 1:
                    cls._unavailable_until.pop(provider_key, None)
                    return parsed
        except Exception:
            pass

        # 旧版 Ollama 不支持批量接口时逐条回退。
        try:
            result = [cls._embed_ollama(text, cfg) for text in materialized]
            cls._unavailable_until.pop(provider_key, None)
            return result
        except Exception as exc:
            cls._mark_unavailable(provider_key)
            raise RuntimeError(f"Ollama batch embedding request failed: {exc}") from exc

    @classmethod
    def _embedding_headers(cls, options):
        key = resolve_option_value(options, 'apiKey', 'api_key')
        return {'Authorization': f'Bearer {key}'} if key else {}

    @classmethod
    def _embed_ollama(cls, text: str, cfg: dict[str, Any]) -> list[float]:
        options = cfg.get("options", {}) if isinstance(cfg.get("options"), dict) else {}
        base_url = _normalize_base_url(resolve_option_value(options, "baseURL", "base_url"))
        model = str(cfg.get("model") or "nomic-embed-text").strip() or "nomic-embed-text"
        provider_key = cls._provider_key(cfg)
        try:
            timeout_seconds = int(os.environ.get("TCODE_EMBEDDING_HTTP_TIMEOUT_SECONDS", "5"))
        except Exception:
            timeout_seconds = 5
        timeout_seconds = max(2, min(timeout_seconds, 30))

        # Prefer modern /api/embed, fallback to legacy /api/embeddings.
        first_error: Optional[Exception] = None
        try:
            data = _post_json(
                f"{base_url}/api/embed",
                {"model": model, "input": [text]},
                timeout=timeout_seconds,
                headers=cls._embedding_headers(options),
            )
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                first = embeddings[0]
                if isinstance(first, list):
                    cls._unavailable_until.pop(provider_key, None)
                    return cls._parse_vector(first)
        except Exception:
            first_error = None

            try:
                data = _post_json(
                    f"{base_url}/api/embeddings",
                    {"model": model, "prompt": text},
                    timeout=timeout_seconds,
                    headers=cls._embedding_headers(options),
                )
                embedding = data.get("embedding")
                if isinstance(embedding, list):
                    cls._unavailable_until.pop(provider_key, None)
                    return cls._parse_vector(embedding)
                first_error = RuntimeError("Ollama embedding response missing vector")
            except Exception as exc:
                first_error = exc

        cls._mark_unavailable(provider_key)
        raise RuntimeError(f"Ollama embedding request failed: {first_error}")
