"""采集 DSH 第一批需要的规模、依赖和隔离性能基线。"""

from __future__ import annotations

import ast
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .common import atomic_write_text, percentile, repo_root, stable_json
from .transcript import default_fixture_root, fixture_summary, load_fixtures, replay_case


SCHEMA = "tcode.dsh-baseline.v1"
DEFAULT_OUTPUT = Path("build/test-reports/dsh-baseline.json")
CORE_ROOTS = (
    Path("tcode_agent/harness"),
    Path("tcode_agent/plugins"),
    Path("tcode_agent/runtime"),
    Path("tcode_agent/session"),
    Path("tcode_agent/tools"),
)


def _python_files(root: Path) -> list[Path]:
    """返回核心运行时目录中的稳定 Python 文件清单。"""

    files: list[Path] = []
    for relative_root in CORE_ROOTS:
        source_root = root / relative_root
        if source_root.is_dir():
            files.extend(
                path
                for path in sorted(source_root.rglob("*.py"))
                if "__pycache__" not in path.parts
            )
    return sorted(set(files))


def _module_name(path: Path, root: Path) -> str:
    """把 Python 文件路径转换为便于比较的模块名称。"""

    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def collect_static_metrics(root: Path) -> dict[str, Any]:
    """统计核心模块行数并建立仅含仓库内部边的静态 import graph。"""

    files = _python_files(root)
    module_lines: dict[str, int] = {}
    edges: set[tuple[str, str]] = set()
    parse_errors: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        module_lines[relative] = len(source.splitlines())
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            parse_errors.append(f"{relative}:{exc.lineno}")
            continue
        source_module = _module_name(path, root)
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package = source_module.split(".")[:-1]
                    keep = max(0, len(package) - (node.level - 1))
                    suffix = node.module.split(".") if node.module else []
                    resolved = ".".join([*package[:keep], *suffix])
                    targets = [resolved] if resolved else []
                elif node.module:
                    targets = [node.module]
            for target in targets:
                if target.startswith("tcode_agent"):
                    edges.add((source_module, target))
    return {
        "source_file_count": len(files),
        "total_lines": sum(module_lines.values()),
        "module_lines": dict(sorted(module_lines.items())),
        "import_edge_count": len(edges),
        "import_edges": [list(edge) for edge in sorted(edges)],
        "parse_errors": parse_errors,
    }


def _measure_process(command: list[str], *, root: Path, timeout: float) -> float:
    """执行一个无 shell 子进程并返回毫秒耗时，失败时抛出明确异常。"""

    environment = dict(os.environ)
    existing = str(environment.get("PYTHONPATH") or "").strip()
    environment["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else str(root)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed = (time.monotonic() - started) * 1000.0
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-1000:]
        raise RuntimeError(f"基线子进程失败：{detail}")
    return elapsed


def _distribution(values: Iterable[float]) -> dict[str, float | int]:
    """把毫秒样本转换为 count、min、median、p95 和 max 分布。"""

    samples = sorted(float(value) for value in values)
    return {
        "count": len(samples),
        "min_ms": round(samples[0], 6) if samples else 0.0,
        "median_ms": round(percentile(samples, 0.5), 6),
        "p95_ms": round(percentile(samples, 0.95), 6),
        "max_ms": round(samples[-1], 6) if samples else 0.0,
    }


def benchmark_cold_import(root: Path, *, iterations: int) -> dict[str, float | int]:
    """以隔离解释器进程测量核心包冷启动导入时间分布。"""

    samples = [
        _measure_process(
            [sys.executable, "-c", "import tcode_agent; print('ok')"],
            root=root,
            timeout=30.0,
        )
        for _ in range(max(1, iterations))
    ]
    return _distribution(samples)


def benchmark_transcript(root: Path, *, iterations: int) -> dict[str, float | int]:
    """测量全部 Transcript Fixture 的规范化回放时间分布。"""

    loaded = load_fixtures(default_fixture_root(root))
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        started = time.perf_counter()
        for _path, case in loaded:
            replay_case(case)
        samples.append((time.perf_counter() - started) * 1000.0)
    return _distribution(samples)


def benchmark_session_projection(root: Path, *, iterations: int) -> dict[str, Any]:
    """显式加载指定仓库并在临时目录测量 Session Event 追加和投影延迟。"""

    normalized_root = str(root.resolve())
    if normalized_root not in sys.path:
        sys.path.insert(0, normalized_root)
    from tcode_agent.session.events import SessionEventStore

    append_samples: list[float] = []
    projection_samples: list[float] = []
    count = max(3, iterations)
    with tempfile.TemporaryDirectory(prefix="tcode-dsh-baseline-") as temporary:
        for index in range(count):
            started = time.perf_counter()
            SessionEventStore.append(
                session_id="baseline-session",
                event_type="baseline.event",
                payload={"index": index},
                workdir=temporary,
            )
            append_samples.append((time.perf_counter() - started) * 1000.0)
            started = time.perf_counter()
            projected = SessionEventStore.list(
                "baseline-session",
                workdir=temporary,
            )
            projection_samples.append((time.perf_counter() - started) * 1000.0)
        final_event_count = len(projected)
    return {
        "sample_kind": "isolated-filesystem-session-event-store",
        "append": _distribution(append_samples),
        "projection": _distribution(projection_samples),
        "final_event_count": final_event_count,
        "production_data_used": False,
    }


def _load_report(path: Path | None, expected_schema: str) -> dict[str, Any] | None:
    """读取可选机器报告，并在 Schema 不匹配或文件缺失时返回空值。"""

    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != expected_schema:
        return None
    return payload


def _smoke_projection(report: dict[str, Any] | None) -> dict[str, Any]:
    """从真实入口报告提取场景耗时和成功率，不复制输出内容。"""

    if report is None:
        return {"available": False, "success": False, "scenarios": []}
    return {
        "available": True,
        "success": bool(report.get("success")),
        "duration_seconds": report.get("duration_seconds", 0.0),
        "scenarios": [
            {
                "scenario": record.get("scenario"),
                "status": record.get("status"),
                "duration_seconds": record.get("duration_seconds", 0.0),
            }
            for record in report.get("records", [])
            if isinstance(record, dict)
        ],
    }


def _test_projection(report: dict[str, Any] | None) -> dict[str, Any]:
    """从有界测试报告提取可比较数量和总耗时，不复制测试输出。"""

    if report is None:
        return {"available": False, "success": False, "counts": {}}
    return {
        "available": True,
        "success": bool(report.get("success")),
        "framework": report.get("framework"),
        "duration_seconds": report.get("duration_seconds", 0.0),
        "selected_module_count": report.get("selected_module_count", 0),
        "counts": dict(report.get("counts") or {}),
    }


def collect_baseline(
    root: Path,
    *,
    iterations: int,
    smoke_report: Path | None,
    test_report: Path | None,
) -> dict[str, Any]:
    """汇总静态规模、隔离性能、真实入口和测试结果为单个基线报告。"""

    fixture = fixture_summary(default_fixture_root(root))
    smoke = _load_report(smoke_report, "tcode.real-entry-smoke-report.v1")
    tests = _load_report(test_report, "tcode.bounded-unittest-report.v1")
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "production_config_used": False,
            "production_data_used": False,
        },
        "static": collect_static_metrics(root),
        "performance": {
            "cold_import": benchmark_cold_import(root, iterations=iterations),
            "transcript_replay": benchmark_transcript(root, iterations=iterations),
            "session_event": benchmark_session_projection(root, iterations=iterations),
            "real_entry": _smoke_projection(smoke),
        },
        "tool_result_distribution": {
            "sample_kind": "versioned-transcript-fixtures",
            "counts": fixture["tool_outcomes"],
        },
        "tests": _test_projection(tests),
        "controls": {
            "thresholds_enforced": False,
            "observational_baseline_only": True,
            "external_llm_used": False,
            "external_mcp_used": False,
        },
    }


def write_baseline(path: Path, baseline: dict[str, Any]) -> None:
    """原子写入基线报告，避免中断时留下半个 JSON 文件。"""

    atomic_write_text(path, stable_json(baseline))


def default_output(root: Path | None = None) -> Path:
    """返回指定仓库下的默认 DSH 基线报告路径。"""

    return (root or repo_root()) / DEFAULT_OUTPUT
