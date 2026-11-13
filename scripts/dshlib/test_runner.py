#!/usr/bin/env python3
"""以隔离子进程运行有界 unittest/pytest 回归并生成机器可读报告。"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


REPORT_SCHEMA = "tcode.bounded-unittest-report.v1"
_WORKER_LIST_PREFIX = "TCODE_BOUNDED_TEST_IDS="


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """保存单个隔离测试目标的有界执行结果。

    属性:
        status: ``passed``、``failed`` 或 ``timeout``。
        return_code: 子进程退出码；硬超时时为 ``None``。
        duration_seconds: 从启动到退出或终止的单调时钟耗时。
        output_tail: 合并标准输出和错误输出后的有界尾部诊断。
    """

    status: str
    return_code: int | None
    duration_seconds: float
    output_tail: str


def _project_root() -> Path:
    """返回脚本所属仓库的绝对根目录。"""

    return Path(__file__).resolve().parents[2]


def _discover_test_modules(project_root: Path) -> list[str]:
    """从 ``tests/test_*.py`` 静态发现并排序 unittest 模块。

    参数:
        project_root: 包含 ``tests`` 目录的仓库根目录。

    返回:
        不导入测试代码即可得到的模块名称列表。
    """

    tests_root = project_root / "tests"
    return [
        f"tests.{path.stem}"
        for path in sorted(tests_root.glob("test_*.py"))
        if path.is_file()
    ]


def _matches_any(value: str, patterns: Iterable[str]) -> bool:
    """判断名称是否匹配任一 shell 风格过滤模式。"""

    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _select_modules(
    modules: Iterable[str],
    *,
    includes: Sequence[str],
    excludes: Sequence[str],
) -> list[str]:
    """应用 include/exclude 规则选择待加载测试模块。

    include 可直接指向模块，也可指向该模块下的类或方法；后者在 test
    粒度展开后会再次精确过滤。exclude 若直接命中模块则跳过整个模块。
    """

    selected: list[str] = []
    for module in modules:
        included = not includes or _matches_any(module, includes) or any(
            pattern.startswith(f"{module}.") for pattern in includes
        )
        excluded = _matches_any(module, excludes)
        if included and not excluded:
            selected.append(module)
    return selected


def _flatten_suite(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    """递归展开 unittest suite，保留 loader 产生的稳定测试顺序。"""

    flattened: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            flattened.extend(_flatten_suite(item))
        else:
            flattened.append(item)
    return flattened


def _list_test_ids(module: str) -> list[str]:
    """导入一个测试模块并返回其中所有稳定 unittest 标识。

    参数:
        module: 形如 ``tests.test_example`` 的模块名。

    异常:
        RuntimeError: unittest loader 在导入或解析模块时记录错误。
    """

    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromName(module)
    if loader.errors:
        raise RuntimeError("\n".join(loader.errors))
    return [test.id() for test in _flatten_suite(suite)]


def _bounded_output(value: str | bytes | None, limit: int = 16_000) -> str:
    """把子进程输出规范为不超过指定字符数的诊断尾部。"""

    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text[-max(1, int(limit)) :]


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> ProcessOutcome:
    """运行单个隔离子进程，并在超时后终止整个目标。

    参数:
        command: 不经过 shell 展开的命令和参数。
        cwd: 子进程工作目录。
        timeout_seconds: 本目标允许的最大墙钟时间。

    返回:
        包含状态、退出码、耗时和有界输出的不可变结果。
    """

    started = time.monotonic()
    environment = dict(os.environ)
    existing_pythonpath = str(environment.get("PYTHONPATH") or "").strip()
    environment["PYTHONPATH"] = (
        f"{cwd}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(cwd)
    )
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(
            timeout=max(0.1, float(timeout_seconds))
        )
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
        combined = "\n".join(
            part for part in (stdout, stderr) if str(part or "").strip()
        )
        return ProcessOutcome(
            status="timeout",
            return_code=None,
            duration_seconds=round(time.monotonic() - started, 6),
            output_tail=_bounded_output(combined),
        )
    combined = "\n".join(
        part for part in (stdout, stderr) if str(part or "").strip()
    )
    return ProcessOutcome(
        status="passed" if process.returncode == 0 else "failed",
        return_code=int(process.returncode),
        duration_seconds=round(time.monotonic() - started, 6),
        output_tail=_bounded_output(combined),
    )


def _worker_test_ids(
    module: str,
    *,
    project_root: Path,
    timeout_seconds: float,
) -> tuple[list[str], ProcessOutcome]:
    """在有界隔离进程中展开一个模块的测试标识。

    返回:
        ``(测试标识列表, 进程结果)``。解析失败时列表为空，进程结果保留
        导入错误、超时或协议错误诊断。
    """

    outcome = _run_process(
        [sys.executable, str(Path(__file__).resolve()), "--worker-list", module],
        cwd=project_root,
        timeout_seconds=timeout_seconds,
    )
    if outcome.status != "passed":
        return [], outcome
    payload_line = next(
        (
            line
            for line in reversed(outcome.output_tail.splitlines())
            if line.startswith(_WORKER_LIST_PREFIX)
        ),
        "",
    )
    try:
        payload = json.loads(payload_line.removeprefix(_WORKER_LIST_PREFIX))
        test_ids = [str(item) for item in payload if str(item).strip()]
    except Exception:
        return [], ProcessOutcome(
            status="failed",
            return_code=outcome.return_code,
            duration_seconds=outcome.duration_seconds,
            output_tail=_bounded_output(
                f"测试枚举子进程未返回合法协议行。\n{outcome.output_tail}"
            ),
        )
    return test_ids, outcome


def _target_command(
    *,
    framework: str,
    target: str,
    module: str,
    project_root: Path,
) -> list[str]:
    """构造 unittest 或 pytest 的无 shell 隔离执行命令。

    参数:
        framework: 已校验的 ``unittest`` 或 ``pytest``。
        target: unittest 模块名或具体测试标识。
        module: 目标所属的 Python 测试模块名。
        project_root: 用于解析 pytest 文件路径的仓库根目录。

    返回:
        可直接交给 ``subprocess`` 的命令参数列表。
    """

    if framework == "pytest":
        module_path = project_root.joinpath(*module.split(".")).with_suffix(".py")
        return [sys.executable, "-m", "pytest", "-q", str(module_path)]
    return [sys.executable, "-m", "unittest", "-v", target]


def _write_report(path: Path, payload: dict[str, object]) -> None:
    """使用同目录临时文件原子写入 UTF-8 JSON 报告。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析协调器参数和内部测试枚举协议参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--granularity",
        choices=("module", "test"),
        default="module",
        help="module 较快；test 为每个测试方法提供独立硬超时",
    )
    parser.add_argument(
        "--framework",
        choices=("unittest", "pytest"),
        default="unittest",
        help="pytest 仅支持 module 粒度，并要求当前 Python 已安装 pytest",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="可重复提供的 shell 风格包含模式",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="可重复提供的 shell 风格排除模式",
    )
    parser.add_argument("--target-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--discovery-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--total-timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("build/test-reports/bounded-unittest.json"),
    )
    parser.add_argument("--worker-list", default="", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _validate_positive_timeout(value: float, name: str) -> float:
    """校验命令行超时为有限正数并返回规范浮点值。"""

    normalized = float(value)
    if normalized <= 0 or normalized != normalized or normalized == float("inf"):
        raise ValueError(f"{name} 必须是有限正数")
    return normalized


def _run_coordinator(args: argparse.Namespace) -> int:
    """按指定粒度协调隔离测试、总截止时间和最终报告。"""

    project_root = _project_root()
    if args.framework == "pytest" and args.granularity != "module":
        raise ValueError("pytest 当前只支持 module 粒度")
    target_timeout = _validate_positive_timeout(
        args.target_timeout_seconds, "target timeout"
    )
    discovery_timeout = _validate_positive_timeout(
        args.discovery_timeout_seconds, "discovery timeout"
    )
    total_timeout = _validate_positive_timeout(
        args.total_timeout_seconds, "total timeout"
    )
    modules = _select_modules(
        _discover_test_modules(project_root),
        includes=tuple(args.include),
        excludes=tuple(args.exclude),
    )
    started_wall = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    deadline = started_monotonic + total_timeout
    records: list[dict[str, object]] = []
    stopped_by_total_timeout = False

    targets: list[tuple[str, str]] = []
    if args.granularity == "module":
        targets = [(module, module) for module in modules]
    else:
        for module in modules:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stopped_by_total_timeout = True
                break
            test_ids, discovery_outcome = _worker_test_ids(
                module,
                project_root=project_root,
                timeout_seconds=min(discovery_timeout, remaining),
            )
            if discovery_outcome.status != "passed":
                records.append(
                    {
                        "kind": "discovery",
                        "module": module,
                        "target": module,
                        **asdict(discovery_outcome),
                    }
                )
                continue
            for test_id in test_ids:
                if args.include and not _matches_any(test_id, args.include):
                    continue
                if _matches_any(test_id, args.exclude):
                    continue
                targets.append((module, test_id))

    for module, target in targets:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stopped_by_total_timeout = True
            break
        print(f"[bounded-unittest] running {target}", flush=True)
        outcome = _run_process(
            _target_command(
                framework=args.framework,
                target=target,
                module=module,
                project_root=project_root,
            ),
            cwd=project_root,
            timeout_seconds=min(target_timeout, remaining),
        )
        if outcome.status == "timeout" and remaining <= target_timeout:
            stopped_by_total_timeout = True
        records.append(
            {
                "kind": args.granularity,
                "module": module,
                "target": target,
                **asdict(outcome),
            }
        )

    counts = {
        status: sum(1 for record in records if record.get("status") == status)
        for status in ("passed", "failed", "timeout")
    }
    success = (
        bool(records)
        and counts["failed"] == 0
        and counts["timeout"] == 0
        and not stopped_by_total_timeout
    )
    finished_wall = datetime.now(timezone.utc)
    payload: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "success": success,
        "framework": args.framework,
        "granularity": args.granularity,
        "started_at": started_wall.isoformat(),
        "finished_at": finished_wall.isoformat(),
        "duration_seconds": round(time.monotonic() - started_monotonic, 6),
        "project_root": str(project_root),
        "timeouts": {
            "target_seconds": target_timeout,
            "discovery_seconds": discovery_timeout,
            "total_seconds": total_timeout,
        },
        "filters": {
            "include": list(args.include),
            "exclude": list(args.exclude),
        },
        "selected_module_count": len(modules),
        "scheduled_target_count": len(targets),
        "completed_target_count": sum(
            1 for record in records if record.get("kind") != "discovery"
        ),
        "stopped_by_total_timeout": stopped_by_total_timeout,
        "counts": counts,
        "records": records,
    }
    report_path = args.report
    if not report_path.is_absolute():
        report_path = project_root / report_path
    _write_report(report_path, payload)
    print(
        "[bounded-unittest] "
        f"success={success} passed={counts['passed']} failed={counts['failed']} "
        f"timeout={counts['timeout']} report={report_path}",
        flush=True,
    )
    return 0 if success else 1


def main(argv: Sequence[str] | None = None) -> int:
    """执行内部枚举 worker 或有界回归协调器。"""

    args = _parse_args(argv)
    if args.worker_list:
        try:
            test_ids = _list_test_ids(str(args.worker_list))
        except Exception as exc:
            print(f"测试枚举失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(
            f"{_WORKER_LIST_PREFIX}{json.dumps(test_ids, ensure_ascii=False)}",
            flush=True,
        )
        return 0
    try:
        return _run_coordinator(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
