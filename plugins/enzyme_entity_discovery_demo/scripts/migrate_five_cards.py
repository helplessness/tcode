#!/usr/bin/env python3
"""One-time migration of an existing validated v1 knowledge bundle."""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from enzyme_ontology_core.bundle import migrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-root", type=Path, default=ROOT / "knowledge")
    parser.add_argument("--output", type=Path, default=ROOT / "knowledge/migration/five-card-import")
    args = parser.parse_args()
    print(json.dumps(migrate(args.knowledge_root, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
