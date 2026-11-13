"""Command line helpers for the workspace evidence brain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .store import (
    EvidenceStore,
    format_evidence_doctor_report,
    format_evidence_graph,
    format_evidence_records,
)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_text_arg(value: str, file_value: str) -> str:
    if file_value:
        return Path(file_value).expanduser().read_text(encoding="utf-8")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tcode_agent.evidence.cli",
        description="Inspect and maintain the local Tcode evidence brain.",
    )
    parser.add_argument("--workdir", default="", help="Workspace directory. Defaults to the current directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    graph = subparsers.add_parser("graph", help="Build or inspect the evidence graph.")
    graph.add_argument("--evidence-id", default="", help="Focus on one evidence neighborhood.")
    graph.add_argument("--query", default="", help="Filter graph nodes by label/type.")
    graph.add_argument("--depth", type=int, default=1, help="Neighborhood depth, 1-3.")
    graph.add_argument("--limit", type=int, default=60, help="Maximum nodes/edges to return.")
    graph.add_argument("--no-persist", action="store_true", help="Do not write graph.json.")
    graph.add_argument("--semantic-hints", action="store_true", help="Include legacy claim/entity hints.")
    graph.add_argument("--json", action="store_true", help="Print raw JSON.")

    doctor = subparsers.add_parser("doctor", help="Run evidence health checks.")
    doctor.add_argument("--evidence-id", default="", help="Check one evidence record.")
    doctor.add_argument("--text", default="", help="Text containing [evidence:<id>] references to validate.")
    doctor.add_argument("--text-file", default="", help="Read referenced text from a file.")
    doctor.add_argument("--no-persist", action="store_true", help="Do not write doctor_report.json.")
    doctor.add_argument("--semantic-hints", action="store_true", help="Include legacy claim/conflict checks.")
    doctor.add_argument("--json", action="store_true", help="Print raw JSON.")
    doctor.add_argument('--verify-assets', action='store_true', help='Hash referenced files; run outside request handlers.')

    search = subparsers.add_parser("search", help="Search evidence records.")
    search.add_argument("query", nargs="?", default="", help="Search query.")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--kind", default="")
    search.add_argument("--source-type", default="")
    search.add_argument("--json", action="store_true", help="Print raw JSON.")

    reindex = subparsers.add_parser('reindex', help='Rebuild derived indexes in resumable batches, without reparsing PDFs.')
    reindex.add_argument('--background', action='store_true', help='Queue maintenance and return immediately; a running platform resumes it.')
    reindex.add_argument('--verify-assets', action='store_true', help='Also verify source file hashes.')
    subparsers.add_parser('index-status', help='Show persisted maintenance and semantic search status.')

    show = subparsers.add_parser("show", help="Show one evidence record.")
    show.add_argument("evidence_id")
    show.add_argument("--json", action="store_true", help="Print raw JSON.")

    status = subparsers.add_parser("status", help="Update an evidence lifecycle status.")
    status.add_argument("evidence_id")
    status.add_argument("value", choices=("active", "stale", "retracted", "superseded"))
    status.add_argument("--json", action="store_true", help="Print raw JSON.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workdir = args.workdir or None

    if args.command == "reindex":
        if args.background or args.verify_assets:
            from .maintenance import request, run, suspend_background
            with suspend_background():
                result = request(workdir=workdir or str(Path.cwd()), verify_assets=args.verify_assets, background=False)
                if not args.background:
                    result = run(workdir=workdir or str(Path.cwd()), wait=True)
                _print_json(result)
        else:
            _print_json(EvidenceStore.rebuild_search_index(workdir=workdir))
        return 0

    if args.command == 'index-status':
        from .maintenance import public_status
        _print_json(public_status(workdir or str(Path.cwd())))
        return 0

    if args.command == "graph":
        if args.evidence_id:
            graph = EvidenceStore.graph_neighborhood(
                args.evidence_id,
                workdir=workdir,
                depth=args.depth,
                limit=args.limit,
                include_semantic_hints=args.semantic_hints,
            )
        else:
            graph = EvidenceStore.build_graph(
                workdir=workdir,
                persist=not args.no_persist,
                include_semantic_hints=args.semantic_hints,
            )
            query = str(args.query or "").strip().lower()
            if query:
                nodes = [
                    node
                    for node in list(graph.get("nodes") or [])
                    if query in str(node.get("label") or "").lower()
                    or query in str(node.get("type") or "").lower()
                ][: max(1, min(100, int(args.limit or 60)))]
                node_ids = {str(node.get("node_id") or "") for node in nodes}
                edges = [
                    edge
                    for edge in list(graph.get("edges") or [])
                    if str(edge.get("source") or "") in node_ids
                    or str(edge.get("target") or "") in node_ids
                ][: max(1, min(100, int(args.limit or 60)))]
                graph = {**graph, "nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}
        _print_json(graph) if args.json else print(format_evidence_graph(graph))
        return 0

    if args.command == "doctor":
        text = _read_text_arg(args.text, args.text_file)
        report = EvidenceStore.doctor(
            workdir=workdir,
            evidence_id=args.evidence_id,
            text=text,
            persist=not args.no_persist,
            include_semantic_hints=args.semantic_hints,
            verify_assets=args.verify_assets,
        )
        _print_json(report) if args.json else print(format_evidence_doctor_report(report))
        return 2 if report.get("status") == "fail" else 1 if report.get("status") == "warn" else 0

    if args.command == "search":
        records = EvidenceStore.search(
            query=args.query,
            workdir=workdir,
            limit=args.limit,
            kind=args.kind,
            source_type=args.source_type,
        )
        _print_json([record.to_dict() for record in records]) if args.json else print(format_evidence_records(records))
        return 0

    if args.command == "show":
        record = EvidenceStore.get(args.evidence_id, workdir=workdir)
        if not record:
            print(f"Evidence not found: {args.evidence_id}", file=sys.stderr)
            return 2
        _print_json(record.to_dict()) if args.json else print(format_evidence_records([record], content_chars=1200))
        return 0

    if args.command == "status":
        record = EvidenceStore.set_status(args.evidence_id, args.value, workdir=workdir)
        if not record:
            print(f"Evidence not found: {args.evidence_id}", file=sys.stderr)
            return 2
        _print_json(record.to_dict()) if args.json else print(
            f"{record.evidence_id}: status={record.status} updated_at={record.updated_at}"
        )
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
