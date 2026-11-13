"""DSH 开发工具使用的确定性文件与统计辅助函数。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable


def repo_root() -> Path:
    """返回 DSH 工具所属仓库的绝对根目录。"""

    return Path(__file__).resolve().parents[2]


def stable_json(value: Any) -> str:
    """把 JSON 兼容对象渲染为键排序且结尾带换行的稳定文本。"""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write_text(path: Path, content: str) -> None:
    """在目标目录内先写临时文件，再原子替换目标文本文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def check_or_write(path: Path, content: str, *, check: bool) -> bool:
    """检查或更新文件，并以布尔值表示目标最终是否与期望一致。"""

    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return True
    if check:
        return False
    atomic_write_text(path, content)
    return True


def source_digest(root: Path, paths: Iterable[Path]) -> tuple[str, list[str]]:
    """计算一组仓库文件的路径和内容摘要，并返回稳定相对路径列表。"""

    normalized = sorted(
        {
            path.resolve()
            for path in paths
            if path.is_file() and path.resolve().is_relative_to(root.resolve())
        },
        key=lambda item: item.relative_to(root.resolve()).as_posix(),
    )
    digest = hashlib.sha256()
    relative_paths: list[str] = []
    for path in normalized:
        relative = path.relative_to(root.resolve()).as_posix()
        relative_paths.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), relative_paths


def percentile(values: Iterable[float], ratio: float) -> float:
    """以最近秩方法计算非空样本的分位值，空样本返回零。"""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    bounded = max(0.0, min(float(ratio), 1.0))
    index = max(0, min(len(ordered) - 1, int((len(ordered) * bounded) + 0.999999) - 1))
    return ordered[index]
