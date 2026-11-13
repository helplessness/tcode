#!/usr/bin/env python3
"""Build an immutable release from the sealed five-card migration input."""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from enzyme_ontology_core.bundle import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, default=ROOT)
    parser.add_argument("--input", type=Path, default=ROOT / "knowledge/migration/five-card-import")
    args = parser.parse_args()
    print(json.dumps(build(args.input, args.plugin_root / "knowledge"), ensure_ascii=False))


if __name__ == "__main__":
    main()
