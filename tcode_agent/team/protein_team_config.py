"""Helpers for reading protein team configuration and peer communication policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).parent / "config" / "protein_team.json"


def load_protein_team_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_peer_policy() -> dict[str, Any]:
    cfg = load_protein_team_config()
    policy = cfg.get("peer_communication")
    if not isinstance(policy, dict):
        policy = {}
    allowed_reasons = [
        str(item).strip()
        for item in (policy.get("allowed_reasons") or [])
        if str(item).strip()
    ]
    if not allowed_reasons:
        allowed_reasons = ["missing_fact", "need_review", "conflict_check"]
    try:
        max_messages = int(policy.get("max_messages_per_pair_per_session", 3) or 3)
    except Exception:
        max_messages = 3
    try:
        timeout_seconds = int(policy.get("round_timeout_seconds", 180) or 180)
    except Exception:
        timeout_seconds = 180
    return {
        "enabled_default": bool(policy.get("enabled_default", False)),
        "allowed_reasons": allowed_reasons,
        "max_messages_per_pair_per_session": max(1, min(12, max_messages)),
        "round_timeout_seconds": max(30, min(3600, timeout_seconds)),
    }


def get_worker_peer_whitelist(worker_name: str) -> list[str]:
    normalized = str(worker_name or "").strip().lower()
    if not normalized:
        return []
    cfg = load_protein_team_config()
    for worker_cfg in cfg.get("workers", []):
        if str(worker_cfg.get("name", "")).strip().lower() != normalized:
            continue
        whitelist = worker_cfg.get("peer_whitelist") or []
        return [
            str(item).strip().lower()
            for item in whitelist
            if str(item).strip()
        ]
    return []
