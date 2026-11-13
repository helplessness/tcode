#!/usr/bin/env python3
"""统一执行 DSH 目录、回放、基线、Smoke 和有界回归。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

# 统一入口必须显式固定仓库根，避免长流程中当前工作目录变化后无法导入项目包。
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dshlib import baseline, catalog, smoke, test_runner, transcript
from dshlib.common import repo_root


def _absolute_path(value: str | Path, *, root: Path) -> Path:
    """把命令行路径解析为以仓库为基准的绝对路径。"""

    path = Path(value)
    return path if path.is_absolute() else root / path


def _catalog_command(args: argparse.Namespace) -> int:
    """生成或检查唯一的 DSH 当前权威目录。"""

    root = Path(args.root).resolve()
    valid, document = catalog.generate(root, check=bool(args.check))
    summary = document["summary"]
    mode = "check" if args.check else "write"
    print(
        "[dsh:catalog] "
        f"mode={mode} valid={valid} tools={summary['static_tool_count']} "
        f"env={summary['environment_key_count']} transcripts={summary['transcript_case_count']}"
    )
    if args.check and not valid:
        print("DSH 当前目录存在漂移；请运行 python scripts/dsh.py catalog。", file=sys.stderr)
    return 0 if valid else 1


def _transcript_command(args: argparse.Namespace) -> int:
    """更新或只读检查全部 Transcript Fixture。"""

    root = Path(args.root).resolve()
    fixture_root = _absolute_path(args.fixtures, root=root)
    if args.action == "update":
        count = transcript.update_fixtures(fixture_root)
        print(f"[dsh:transcript] action=update cases={count} valid=True")
        return 0
    valid, errors = transcript.check_fixtures(fixture_root)
    for error in errors:
        print(error, file=sys.stderr)
    summary = transcript.fixture_summary(fixture_root)
    print(
        "[dsh:transcript] "
        f"action=check cases={summary['case_count']} events={summary['event_count']} valid={valid}"
    )
    return 0 if valid else 1


def _smoke_arguments(args: argparse.Namespace) -> list[str]:
    """把统一 CLI 的 Smoke 参数投影给共享真实入口执行器。"""

    forwarded = [
        "--scenario-timeout-seconds",
        str(args.scenario_timeout_seconds),
        "--total-timeout-seconds",
        str(args.total_timeout_seconds),
        "--report",
        str(args.report),
    ]
    for scenario in args.scenario:
        forwarded.extend(["--scenario", scenario])
    return forwarded


def _smoke_command(args: argparse.Namespace) -> int:
    """通过统一入口执行隔离 Server、CLI、Plugin、MCP 与 Worker Smoke。"""

    return smoke.main(_smoke_arguments(args))


def _test_arguments(args: argparse.Namespace) -> list[str]:
    """把统一 CLI 的测试参数投影给有界测试协调器。"""

    forwarded = [
        "--framework",
        args.framework,
        "--granularity",
        args.granularity,
        "--target-timeout-seconds",
        str(args.target_timeout_seconds),
        "--discovery-timeout-seconds",
        str(args.discovery_timeout_seconds),
        "--total-timeout-seconds",
        str(args.total_timeout_seconds),
        "--report",
        str(args.report),
    ]
    for pattern in args.include:
        forwarded.extend(["--include", pattern])
    for pattern in args.exclude:
        forwarded.extend(["--exclude", pattern])
    return forwarded


def _test_command(args: argparse.Namespace) -> int:
    """通过统一入口执行模块或测试粒度的有界回归。"""

    return test_runner.main(_test_arguments(args))


def _baseline_command(args: argparse.Namespace) -> int:
    """采集隔离性能、静态规模、真实入口和测试结果基线。"""

    root = Path(args.root).resolve()
    output = _absolute_path(args.output, root=root)
    smoke_report = _absolute_path(args.smoke_report, root=root) if args.smoke_report else None
    test_report = _absolute_path(args.test_report, root=root) if args.test_report else None
    document = baseline.collect_baseline(
        root,
        iterations=max(1, int(args.iterations)),
        smoke_report=smoke_report,
        test_report=test_report,
    )
    baseline.write_baseline(output, document)
    performance = document["performance"]
    print(
        "[dsh:baseline] "
        f"output={output} import_p95_ms={performance['cold_import']['p95_ms']} "
        f"projection_p95_ms={performance['session_event']['projection']['p95_ms']}"
    )
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    """依次执行只读回放、目录漂移、真实入口、回归与基线总门禁。"""

    root = Path(args.root).resolve()
    fixture_root = transcript.default_fixture_root(root)
    valid, errors = transcript.check_fixtures(fixture_root)
    for error in errors:
        print(error, file=sys.stderr)
    if not valid:
        return 1
    catalog_valid, _document = catalog.generate(root, check=True)
    if not catalog_valid:
        print("DSH 当前目录存在漂移。", file=sys.stderr)
        return 1

    smoke_report = root / "build/test-reports/dsh-real-entry-smoke.json"
    test_report = root / "build/test-reports/dsh-pytest.json"
    if not args.skip_smoke:
        smoke_result = smoke.main(
            [
                "--scenario-timeout-seconds",
                str(args.scenario_timeout_seconds),
                "--total-timeout-seconds",
                str(args.smoke_timeout_seconds),
                "--report",
                str(smoke_report),
            ]
        )
        if smoke_result != 0:
            return smoke_result
    if not args.skip_tests:
        test_result = test_runner.main(
            [
                "--framework",
                "pytest",
                "--granularity",
                "module",
                "--target-timeout-seconds",
                str(args.test_module_timeout_seconds),
                "--total-timeout-seconds",
                str(args.test_total_timeout_seconds),
                "--report",
                str(test_report),
            ]
        )
        if test_result != 0:
            return test_result

    output = root / "build/test-reports/dsh-baseline.json"
    document = baseline.collect_baseline(
        root,
        iterations=max(1, int(args.baseline_iterations)),
        smoke_report=smoke_report if smoke_report.is_file() else None,
        test_report=test_report if test_report.is_file() else None,
    )
    baseline.write_baseline(output, document)
    print(
        "[dsh:verify] success=True "
        f"smoke={'skipped' if args.skip_smoke else 'passed'} "
        f"tests={'skipped' if args.skip_tests else 'passed'} baseline={output}"
    )
    return 0


def _add_common_root(parser: argparse.ArgumentParser) -> None:
    """为子命令增加统一仓库根参数。"""

    parser.add_argument("--root", default=str(repo_root()))


def _add_smoke_options(parser: argparse.ArgumentParser) -> None:
    """为子命令增加统一真实入口场景和时限参数。"""

    parser.add_argument("--scenario", action="append", choices=smoke.SCENARIO_IDS, default=[])
    parser.add_argument("--scenario-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--total-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--report", default="build/test-reports/dsh-real-entry-smoke.json")


def _add_test_options(parser: argparse.ArgumentParser) -> None:
    """为子命令增加统一测试框架、粒度、过滤和时限参数。"""

    parser.add_argument("--framework", choices=("unittest", "pytest"), default="pytest")
    parser.add_argument("--granularity", choices=("module", "test"), default="module")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--target-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--discovery-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--total-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--report", default="build/test-reports/dsh-pytest.json")


def _build_parser() -> argparse.ArgumentParser:
    """构建只暴露五个稳定子命令的统一 DSH 参数解析器。"""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog_parser = subparsers.add_parser("catalog", help="生成或检查当前权威目录")
    _add_common_root(catalog_parser)
    catalog_parser.add_argument("--check", action="store_true")
    catalog_parser.set_defaults(handler=_catalog_command)

    transcript_parser = subparsers.add_parser("transcript", help="更新或检查 Transcript")
    _add_common_root(transcript_parser)
    transcript_parser.add_argument("action", choices=("check", "update"))
    transcript_parser.add_argument("--fixtures", default=str(transcript.DEFAULT_FIXTURE_ROOT))
    transcript_parser.set_defaults(handler=_transcript_command)

    smoke_parser = subparsers.add_parser("smoke", help="执行隔离真实入口门禁")
    _add_smoke_options(smoke_parser)
    smoke_parser.set_defaults(handler=_smoke_command)

    test_parser = subparsers.add_parser("test", help="执行有界回归")
    _add_test_options(test_parser)
    test_parser.set_defaults(handler=_test_command)

    baseline_parser = subparsers.add_parser("baseline", help="采集规模与性能基线")
    _add_common_root(baseline_parser)
    baseline_parser.add_argument("--iterations", type=int, default=5)
    baseline_parser.add_argument("--smoke-report", default="")
    baseline_parser.add_argument("--test-report", default="")
    baseline_parser.add_argument("--output", default=str(baseline.DEFAULT_OUTPUT))
    baseline_parser.set_defaults(handler=_baseline_command)

    verify_parser = subparsers.add_parser("verify", help="执行第一批完整总门禁")
    _add_common_root(verify_parser)
    verify_parser.add_argument("--skip-smoke", action="store_true")
    verify_parser.add_argument("--skip-tests", action="store_true")
    verify_parser.add_argument("--scenario-timeout-seconds", type=float, default=45.0)
    verify_parser.add_argument("--smoke-timeout-seconds", type=float, default=120.0)
    verify_parser.add_argument("--test-module-timeout-seconds", type=float, default=180.0)
    verify_parser.add_argument("--test-total-timeout-seconds", type=float, default=3600.0)
    verify_parser.add_argument("--baseline-iterations", type=int, default=5)
    verify_parser.set_defaults(handler=_verify_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析统一命令并把可预期输入错误转换为稳定退出码。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DSH 命令失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
