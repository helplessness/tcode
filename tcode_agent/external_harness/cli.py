"""`tcode runtimes` 的发现、信任、诊断与 conformance 命令。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Sequence

from .profile_loader import load_profile
from .registry import runtime_registry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tcode runtimes",
        description="发现和管理外部 Agent Harness",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("discover", "list"):
        listing = sub.add_parser(name)
        listing.add_argument("--json", action="store_true")
    validate = sub.add_parser("validate")
    validate.add_argument("profile")
    validate.add_argument("--json", action="store_true")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("runtime_id")
    inspect.add_argument("--json", action="store_true")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("runtime_id")
    doctor.add_argument("--json", action="store_true")
    for action in ("enable", "disable"):
        command = sub.add_parser(action)
        command.add_argument("runtime_id")
        command.add_argument("--json", action="store_true")
    conformance = sub.add_parser("conformance")
    conformance.add_argument("runtime_id")
    mode = conformance.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--live", action="store_true")
    conformance.add_argument("--workdir", default="")
    conformance.add_argument("--json", action="store_true")
    return parser


def _print(value: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, dict):
            print(item)
            continue
        runtime_id = item.get("runtimeId") or item.get("runtime_id") or "-"
        state = item.get("compatibilityState") or item.get("available")
        print(
            f"{runtime_id}: source={item.get('sourceType', '-')} "
            f"enabled={item.get('enabled', False)} state={state}"
        )
        for diagnostic in item.get("diagnostics") or []:
            print(f"  - {diagnostic}")


def _record_payload(*, include_definition: bool) -> list[dict[str, Any]]:
    return [
        item.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude=set() if include_definition else {"definition"},
        )
        for item in runtime_registry.records()
    ]


async def _run(args: argparse.Namespace) -> int:
    json_output = bool(getattr(args, "json", False))
    if args.action in {"discover", "list"}:
        _print(_record_payload(include_definition=False), json_output=json_output)
        return 0

    if args.action == "validate":
        try:
            loaded = load_profile(Path(args.profile))
            payload = {
                "valid": True,
                "path": loaded.path.as_posix(),
                "definitionDigest": loaded.digest,
                "runtime": loaded.definition.model_dump(
                    mode="json", by_alias=True
                ),
            }
        except Exception as exc:
            payload = {
                "valid": False,
                "path": str(Path(args.profile).expanduser()),
                "diagnostics": [str(exc)],
            }
            _print(payload, json_output=json_output)
            return 2
        _print(payload, json_output=json_output)
        return 0

    if args.action == "inspect":
        records = [
            item
            for item in _record_payload(include_definition=True)
            if item.get("runtimeId") == args.runtime_id
        ]
        if not records:
            _print(
                {
                    "runtimeId": args.runtime_id,
                    "diagnostics": ["unknown runtime"],
                },
                json_output=json_output,
            )
            return 2
        _print(records[0] if len(records) == 1 else records, json_output=json_output)
        return 0

    if args.action in {"enable", "disable"}:
        enabled = args.action == "enable"
        try:
            runtime_registry.set_enabled(args.runtime_id, enabled)
        except KeyError:
            _print(
                {
                    "runtimeId": args.runtime_id,
                    "enabled": False,
                    "diagnostics": ["unknown runtime"],
                },
                json_output=json_output,
            )
            return 2
        record = next(
            (
                item
                for item in runtime_registry.records()
                if item.runtime_id == args.runtime_id
            ),
            None,
        )
        payload = {
            "runtimeId": args.runtime_id,
            "enabled": enabled,
            "trustState": (
                "trusted"
                if enabled and record and record.source_type == "entrypoint"
                else record.trust_state if record else "unknown"
            ),
            "restartRequired": bool(
                record and record.source_type == "entrypoint"
            ),
            "diagnostics": (
                [
                    "Python entry point trust recorded; restart the TCode "
                    "service to load or unload provider code"
                ]
                if record and record.source_type == "entrypoint"
                else []
            ),
        }
        _print(payload, json_output=json_output)
        return 0

    if args.action == "doctor":
        probes = [
            item
            for item in await runtime_registry.probes(include_models=True)
            if item.runtime_id == args.runtime_id
        ]
        if not probes:
            _print(
                {
                    "runtimeId": args.runtime_id,
                    "diagnostics": ["unknown runtime"],
                },
                json_output=json_output,
            )
            return 2
        payload = probes[0].model_dump(mode="json", by_alias=True)
        _print(payload, json_output=json_output)
        return 0 if probes[0].available and probes[0].enabled else 1

    if args.action == "conformance":
        from ..runtime.gateway import agent_backend_registry
        from .conformance import run_conformance

        report = await run_conformance(
            args.runtime_id,
            runtime_registry=runtime_registry,
            backend_registry=agent_backend_registry,
            live=bool(args.live),
            workdir=args.workdir,
        )
        record = runtime_registry.get_record(args.runtime_id)
        evidence_path = ""
        if record is not None and record.definition_digest:
            evidence_path = runtime_registry.conformance_store.write(
                report,
                definition_digest=record.definition_digest,
            ).as_posix()
        payload = report.model_dump(mode="json", by_alias=True)
        payload["evidencePath"] = evidence_path
        _print(payload, json_output=True if json_output else False)
        return 0 if report.passed else 1
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run(_parser().parse_args(argv)))


__all__ = ["main"]
