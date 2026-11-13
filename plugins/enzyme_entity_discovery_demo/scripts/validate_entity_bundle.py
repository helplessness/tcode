#!/usr/bin/env python3
"""Validate all release hashes, scientific references and source quotes using SQL."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from enzyme_ontology_core.bundle import validate as validate_root


def validate():
    return validate_root(ROOT / "knowledge")


if __name__ == "__main__":
    print(json.dumps(validate(), ensure_ascii=False))
