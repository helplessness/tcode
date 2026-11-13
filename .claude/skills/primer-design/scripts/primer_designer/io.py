"""序列文件和突变清单文件的读取工具。"""

from __future__ import annotations

import csv
from pathlib import Path


def read_sequence_file(path: str | Path) -> str:
    """读取 FASTA 或纯 DNA 文本，具体清洗工作由 ``normalize_dna`` 完成。"""

    return Path(path).read_text(encoding="utf-8-sig")


def read_mutations_file(path: str | Path) -> list[str]:
    """读取 TXT 或 CSV 格式的突变清单。

    CSV 优先读取名为 ``mutation`` 的列；若不存在，则读取第一列。
    TXT 按行读取，并忽略空行和以 ``#`` 开头的说明行。
    """

    source = Path(path)
    text = source.read_text(encoding="utf-8-sig")
    if source.suffix.lower() == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
        if not rows:
            return []
        field = "mutation" if "mutation" in rows[0] else next(iter(rows[0]))
        return [row[field].strip() for row in rows if row.get(field, "").strip()]
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
