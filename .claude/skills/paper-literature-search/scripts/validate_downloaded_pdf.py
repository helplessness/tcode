#!/usr/bin/env python3
"""Validate that a downloaded paper file is a real PDF, not an HTML error page."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


HTML_MARKERS = (
    b"<html",
    b"<!doctype",
    b"<head",
    b"<body",
    b"access denied",
    b"just a moment",
    b"cf-browser-verification",
    b"cloudflare",
)


def _read_head_tail(path: Path) -> tuple[bytes, bytes, int]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(8192)
        if size <= 65536:
            tail = head + handle.read()
        else:
            handle.seek(max(0, size - 65536))
            tail = handle.read(65536)
    return head, tail, size


def validate_pdf(path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "ok": False,
        "path": str(path),
        "exists": path.exists(),
        "size": 0,
        "has_pdf_suffix": path.suffix.lower() == ".pdf",
        "header_offset": None,
        "has_startxref": False,
        "has_eof_marker": False,
        "html_markers_detected": [],
        "errors": [],
    }

    if not path.exists():
        result["errors"].append("file_not_found")
        return result
    if not path.is_file():
        result["errors"].append("not_a_file")
        return result

    head, tail, size = _read_head_tail(path)
    result["size"] = size
    if size <= 0:
        result["errors"].append("empty_file")
        return result

    if path.suffix.lower() != ".pdf":
        result["errors"].append("missing_pdf_suffix")

    lowered_head = head.lower()
    header_offset = head.find(b"%PDF-")
    if header_offset == -1 or header_offset > 1024:
        result["errors"].append("missing_pdf_header")
    else:
        result["header_offset"] = header_offset

    markers = [marker.decode("ascii", "ignore") for marker in HTML_MARKERS if marker in lowered_head]
    if markers:
        result["html_markers_detected"] = markers
        result["errors"].append("html_markers_detected")

    if b"startxref" in tail:
        result["has_startxref"] = True
    else:
        result["errors"].append("missing_startxref")

    if b"%%EOF" in tail:
        result["has_eof_marker"] = True
    else:
        result["errors"].append("missing_eof_marker")

    result["ok"] = not result["errors"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate that a downloaded paper file is a real PDF."
    )
    parser.add_argument("path", help="Path to the downloaded PDF file")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON result",
    )
    args = parser.parse_args()

    path = Path(os.path.expanduser(args.path)).resolve()
    result = validate_pdf(path)
    dump_kwargs = {"ensure_ascii": False, "sort_keys": True}
    if args.pretty:
        dump_kwargs["indent"] = 2
    print(json.dumps(result, **dump_kwargs))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())