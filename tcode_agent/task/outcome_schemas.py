"""Load and validate the public Outcome v2 and Proof Pack v1 schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


_CONTRACTS_DIR = Path(__file__).with_name("contracts")


def _load_schema(name: str) -> dict[str, Any]:
    path = _CONTRACTS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def load_outcome_v2_schema() -> dict[str, Any]:
    return _load_schema("outcome_v2.schema.json")


def load_proof_pack_v1_schema() -> dict[str, Any]:
    return _load_schema("proof_pack_v1.schema.json")


def validate_outcome_v2(value: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(load_outcome_v2_schema())
    return [
        error.message
        for error in sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.path),
        )
    ]


def validate_proof_pack_v1(value: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(load_proof_pack_v1_schema())
    return [
        error.message
        for error in sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.path),
        )
    ]
