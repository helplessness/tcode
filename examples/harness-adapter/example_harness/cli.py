"""Small NDJSON Harness used by the public Adapter example."""

from __future__ import annotations

import json
import sys


def main() -> int:
    prompt = sys.stdin.read()
    if len(prompt.encode("utf-8")) > 2_000_000:
        print(json.dumps({"error": "prompt too large"}), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"delta": f"Example Harness: {prompt}"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
