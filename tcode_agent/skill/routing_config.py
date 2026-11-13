"""Skill 路由配置加载。

业务标签和领域定义来自 YAML；代码只提供算法默认值与配置校验。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config.manager import ConfigManager


DEFAULT_SKILL_ROUTING_CONFIG: dict[str, Any] = {
    "version": 1,
    "enabled": True,
    "catalog": {
        "inherit_parent_skills": True,
        "include_home_skills": True,
        "duplicate_policy": "keep-last",
        "write_snapshot": True,
        "snapshot_path": ".tcode/skill-routing/catalog.json",
    },
    "routing": {
        "mode": "active",
        "candidate_limit": 6,
        "recommendation_limit": 3,
        "recommendation_min_score": 0.16,
        "selection_min_score": 0.32,
        "selection_min_margin": 0.06,
        "auto_load_min_score": 0.58,
        "negative_penalty_weight": 0.25,
        "priority_weight": 0.03,
        "include_recall_context": False,
        "weights": {
            "rule": 0.40,
            "bm25": 0.30,
            "vector": 0.25,
            "domain": 0.05,
        },
        "stop_terms": [],
    },
    "vector": {
        "enabled": True,
        "dimensions": 256,
        "index_path": ".tcode/skill-routing/vector-index.json",
    },
    "trace": {
        "enabled": False,
        "include_query": False,
        "path": ".tcode/skill-routing/traces.jsonl",
        "max_candidates": 10,
    },
    "composite": {
        "enabled": False,
        "mode": "off",
        "max_steps": 6,
        "min_steps": 2,
        "min_distinct_skills": 2,
        "min_step_score": 0.30,
        "include_unrouted_steps": True,
        "expose_to_agent": False,
    },
    "domains": {},
}


_CONFIG_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = {key: copy.deepcopy(value) for key, value in base.items()}
        for key, value in override.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return copy.deepcopy(override)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def resolve_skill_routing_config_path(workdir: Optional[str] = None) -> Optional[Path]:
    """返回实际使用的 Skill 路由配置文件。"""

    base = Path(workdir or os.getcwd()).expanduser().resolve()
    if base.is_file():
        base = base.parent

    explicit = str(os.environ.get("TCODE_SKILL_ROUTING_CONFIG", "") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = base / path
        return path.resolve()

    try:
        config_root = Path(ConfigManager.get_effective_config_root(str(base))).resolve()
    except Exception:
        config_root = base

    package_root = Path(__file__).resolve().parents[2]
    candidates = [
        config_root / "skill_routing.yaml",
        package_root / "config" / "templates" / "skill_routing.example.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _validate(config: dict[str, Any]) -> dict[str, Any]:
    catalog = config.setdefault("catalog", {})
    duplicate_policy = str(catalog.get("duplicate_policy") or "keep-last").strip().lower()
    if duplicate_policy not in {"keep-first", "keep-last", "error"}:
        duplicate_policy = "keep-last"
    catalog["duplicate_policy"] = duplicate_policy

    routing = config.setdefault("routing", {})
    mode = str(routing.get("mode") or "active").strip().lower()
    if mode not in {"active", "shadow", "legacy"}:
        mode = "active"
    routing["mode"] = mode
    for key, minimum, maximum in (
        ("candidate_limit", 1, 50),
        ("recommendation_limit", 1, 10),
    ):
        try:
            routing[key] = max(minimum, min(maximum, int(routing.get(key, minimum))))
        except Exception:
            routing[key] = minimum

    for key, default in (
        ("recommendation_min_score", 0.16),
        ("selection_min_score", 0.32),
        ("selection_min_margin", 0.06),
        ("auto_load_min_score", 0.58),
        ("negative_penalty_weight", 0.25),
        ("priority_weight", 0.03),
    ):
        try:
            routing[key] = max(0.0, min(1.0, float(routing.get(key, default))))
        except Exception:
            routing[key] = default

    weights = routing.setdefault("weights", {})
    normalized_weights: dict[str, float] = {}
    for key, default in (("rule", 0.40), ("bm25", 0.30), ("vector", 0.25), ("domain", 0.05)):
        try:
            normalized_weights[key] = max(0.0, float(weights.get(key, default)))
        except Exception:
            normalized_weights[key] = default
    total = sum(normalized_weights.values()) or 1.0
    routing["weights"] = {key: value / total for key, value in normalized_weights.items()}

    stop_terms = routing.get("stop_terms", [])
    if isinstance(stop_terms, str):
        stop_terms = [part.strip() for part in stop_terms.replace("，", ",").split(",")]
    routing["stop_terms"] = [str(item).strip().lower() for item in stop_terms or [] if str(item).strip()]

    if not isinstance(config.get("domains"), dict):
        config["domains"] = {}

    composite = config.setdefault("composite", {})
    composite_mode = str(composite.get("mode") or "off").strip().lower()
    if composite_mode not in {"off", "shadow", "active"}:
        composite_mode = "off"
    composite["mode"] = composite_mode
    composite["enabled"] = bool(composite.get("enabled", False))
    composite["include_unrouted_steps"] = bool(composite.get("include_unrouted_steps", True))
    composite["expose_to_agent"] = bool(composite.get("expose_to_agent", composite_mode == "active"))
    for key, minimum, maximum, default in (
        ("max_steps", 2, 12, 6),
        ("min_steps", 2, 12, 2),
        ("min_distinct_skills", 2, 12, 2),
    ):
        try:
            composite[key] = max(minimum, min(maximum, int(composite.get(key, default))))
        except Exception:
            composite[key] = default
    composite["min_steps"] = min(int(composite["min_steps"]), int(composite["max_steps"]))
    composite["min_distinct_skills"] = min(
        int(composite["min_distinct_skills"]),
        int(composite["max_steps"]),
    )
    try:
        composite["min_step_score"] = max(
            0.0,
            min(1.0, float(composite.get("min_step_score", 0.30))),
        )
    except Exception:
        composite["min_step_score"] = 0.30
    return config


def load_skill_routing_config(workdir: Optional[str] = None) -> dict[str, Any]:
    """加载配置并合并安全默认值；文件不存在时仍返回可用配置。"""

    config = copy.deepcopy(DEFAULT_SKILL_ROUTING_CONFIG)
    path = resolve_skill_routing_config_path(workdir)
    if path and path.is_file():
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = -1
        cache_key = str(path)
        cached = _CONFIG_CACHE.get(cache_key)
        if cached and cached[0] == mtime:
            config = copy.deepcopy(cached[1])
        else:
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                raise ValueError(f"无法读取 Skill 路由配置 {path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"Skill 路由配置必须是 YAML 对象: {path}")
            config = _validate(_deep_merge(config, loaded))
            config["_config_path"] = str(path)
            _CONFIG_CACHE[cache_key] = (mtime, copy.deepcopy(config))

    config = _validate(config)
    config["enabled"] = _env_bool("TCODE_SKILL_ROUTING_ENABLED", bool(config.get("enabled", True)))
    trace = config.setdefault("trace", {})
    trace["enabled"] = _env_bool(
        "TCODE_SKILL_ROUTING_TRACE_ENABLED",
        bool(trace.get("enabled", False)),
    )
    return config


def resolve_skill_routing_output_path(
    value: str,
    *,
    workdir: Optional[str] = None,
) -> Path:
    """把配置里的相对输出路径解析到当前 workspace。"""

    path = Path(str(value or "").strip()).expanduser()
    if path.is_absolute():
        return path.resolve()
    base = Path(workdir or os.getcwd()).expanduser().resolve()
    if base.is_file():
        base = base.parent
    return (base / path).resolve()


def clear_skill_routing_config_cache() -> None:
    _CONFIG_CACHE.clear()
