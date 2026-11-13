"""提供无需模型密钥的 Transcript 规范化、更新和只读回放。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .common import atomic_write_text, repo_root, stable_json


SCHEMA = "tcode.transcript-fixture.v1"
DEFAULT_FIXTURE_ROOT = Path("tests/fixtures/transcripts")
_IDENTITY_FIELDS = {
    "id",
    "event_id",
    "session_id",
    "message_id",
    "parent_id",
    "call_id",
    "request_id",
    "turn_id",
    "trace_id",
    "job_id",
}
_TIME_FIELDS = {
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "timestamp",
}
_PATH_FIELDS = {"workdir", "cwd", "output_path", "checkpoint_path"}


@dataclass
class TranscriptNormalizer:
    """在单个场景内把动态身份、时间、路径和工具参数变成稳定投影。"""

    aliases: dict[tuple[str, str], str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def _identity_alias(self, field_name: str, value: Any) -> str:
        """按首次出现顺序为动态身份分配与原始内容无关的占位符。"""

        text = str(value or "")
        key = (field_name, text)
        if key not in self.aliases:
            count = self.counters.get(field_name, 0) + 1
            self.counters[field_name] = count
            self.aliases[key] = f"<{field_name}:{count}>"
        return self.aliases[key]

    def normalize(self, value: Any, *, field_name: str = "") -> Any:
        """递归规范化一个 JSON 值，同时保留所有业务可观察字段。"""

        if field_name in _IDENTITY_FIELDS and value not in (None, ""):
            return self._identity_alias(field_name, value)
        if field_name in _TIME_FIELDS and value not in (None, ""):
            return "<timestamp>"
        if field_name in _PATH_FIELDS and value not in (None, ""):
            return "<path>"
        if isinstance(value, dict):
            return {
                str(key): self.normalize(item, field_name=str(key))
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, list):
            return [self.normalize(item) for item in value]
        if field_name in {"arguments", "tool_arguments"} and isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return value
            return self.normalize(parsed, field_name=field_name)
        return value


def _fixture_paths(root: Path) -> list[Path]:
    """返回 Fixture 目录内按路径排序的 JSONL 文件。"""

    return sorted(path for path in root.glob("*.jsonl") if path.is_file())


def load_fixture_file(path: Path) -> list[dict[str, Any]]:
    """读取一个 JSONL Fixture，并对行号和最小 Schema 给出明确错误。"""

    cases: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            case = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} 不是合法 JSON：{exc}") from exc
        if not isinstance(case, dict):
            raise ValueError(f"{path}:{line_number} 必须是 JSON 对象")
        if case.get("schema") != SCHEMA:
            raise ValueError(f"{path}:{line_number} 的 schema 不受支持")
        case_id = str(case.get("case_id") or "").strip()
        events = case.get("events")
        expected = case.get("expected")
        if not case_id or not isinstance(events, list) or not isinstance(expected, list):
            raise ValueError(f"{path}:{line_number} 缺少 case_id、events 或 expected")
        cases.append(case)
    return cases


def load_fixtures(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """读取目录中的全部 Fixture，并拒绝跨文件重复场景标识。"""

    loaded: list[tuple[Path, dict[str, Any]]] = []
    identifiers: set[str] = set()
    for path in _fixture_paths(root):
        for case in load_fixture_file(path):
            case_id = str(case["case_id"])
            if case_id in identifiers:
                raise ValueError(f"Transcript case_id 重复：{case_id}")
            identifiers.add(case_id)
            loaded.append((path, case))
    if not loaded:
        raise ValueError(f"没有找到 Transcript Fixture：{root}")
    return loaded


def replay_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    """重放一个已记录场景，并返回与运行期动态值解耦的事件序列。"""

    normalizer = TranscriptNormalizer()
    replayed = normalizer.normalize(case["events"])
    if not isinstance(replayed, list) or not all(isinstance(item, dict) for item in replayed):
        raise ValueError(f"Transcript 场景 {case.get('case_id')} 的 events 必须是对象数组")
    return replayed


def check_fixtures(root: Path) -> tuple[bool, list[str]]:
    """只读检查全部场景的期望投影和 JSONL 字节格式。"""

    errors: list[str] = []
    by_path: dict[Path, list[dict[str, Any]]] = {}
    for path, case in load_fixtures(root):
        replayed = replay_case(case)
        if replayed != case["expected"]:
            errors.append(f"{case['case_id']} 的回放结果与 expected 不一致")
        by_path.setdefault(path, []).append(case)
    for path, cases in by_path.items():
        expected_text = render_fixture_file(cases)
        if path.read_text(encoding="utf-8") != expected_text:
            errors.append(f"{path} 不是规范 JSONL 字节格式")
    return not errors, errors


def render_fixture_file(cases: Iterable[dict[str, Any]]) -> str:
    """以每行一个紧凑 JSON 对象的方式渲染稳定 Fixture 内容。"""

    return "".join(
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for case in cases
    )


def update_fixtures(root: Path) -> int:
    """重新计算并原子写入全部 Fixture 的 expected 投影。"""

    by_path: dict[Path, list[dict[str, Any]]] = {}
    for path, case in load_fixtures(root):
        updated = dict(case)
        updated["expected"] = replay_case(case)
        by_path.setdefault(path, []).append(updated)
    for path, cases in by_path.items():
        atomic_write_text(path, render_fixture_file(cases))
    return sum(len(cases) for cases in by_path.values())


def fixture_summary(root: Path) -> dict[str, Any]:
    """返回场景数量、事件数量、结果分布和 Fixture 内容摘要。"""

    loaded = load_fixtures(root)
    outcomes = {"success": 0, "denied": 0, "timeout": 0, "other": 0}
    digest = hashlib.sha256()
    files: list[str] = []
    event_count = 0
    for path in _fixture_paths(root):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        files.append(path.relative_to(repo_root()).as_posix())
    for _path, case in loaded:
        events = replay_case(case)
        event_count += len(events)
        for event in events:
            if event.get("kind") != "tool.result":
                continue
            status = str(event.get("payload", {}).get("status") or "other")
            outcomes[status if status in outcomes else "other"] += 1
    return {
        "schema": SCHEMA,
        "case_count": len(loaded),
        "event_count": event_count,
        "tool_outcomes": outcomes,
        "source_digest": digest.hexdigest(),
        "source_files": files,
    }


def default_fixture_root(root: Path | None = None) -> Path:
    """返回指定仓库下的默认 Transcript Fixture 目录。"""

    return (root or repo_root()) / DEFAULT_FIXTURE_ROOT


def render_summary(summary: dict[str, Any]) -> str:
    """渲染便于调试 Transcript 摘要的稳定 JSON。"""

    return stable_json(summary)
