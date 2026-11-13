#!/usr/bin/env python3
"""读取 primer_designer 的 CSV/XLSX 输出，并汇总每个位点的首选引物对。"""

from __future__ import annotations

import argparse
import csv
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """按扩展名读取 CSV 或 XLSX，并返回表头到单元格值的映射。"""

    if path.suffix.lower() == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ModuleNotFoundError as exc:
            raise RuntimeError("读取 XLSX 需要 openpyxl，请使用项目指定的 tf 环境") from exc
        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        values = sheet.iter_rows(values_only=True)
        try:
            headers = [str(value) for value in next(values)]
        except StopIteration:
            return []
        return [dict(zip(headers, row)) for row in values if any(value is not None for value in row)]

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(value: Any) -> int | float | str | None:
    """把表格中的数字规范为 JSON 数值，同时保留无法转换的文本。"""

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def summarize(path: Path, *, fallback_near_miss: bool = False) -> dict[str, Any]:
    """按突变分组，选择 rank=1 的 PASS；没有 PASS 时可回退到 NEAR_MISS。"""

    rows = _read_rows(path)
    required = {"mutation", "candidate_rank", "direction", "status", "sequence_5to3"}
    if rows and not required.issubset(rows[0]):
        missing = ", ".join(sorted(required - set(rows[0])))
        raise ValueError(f"不是有效的 primer_designer 输出，缺少字段：{missing}")

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        grouped.setdefault(str(row["mutation"]), []).append(row)

    mutations: list[dict[str, Any]] = []
    pass_count = 0
    for mutation, mutation_rows in grouped.items():
        pass_rows = [
            row for row in mutation_rows
            if str(row.get("status")) == "PASS" and _number(row.get("candidate_rank")) == 1
        ]
        selected = pass_rows
        if not selected and fallback_near_miss:
            selected = [
                row for row in mutation_rows
                if str(row.get("status")) == "NEAR_MISS"
                and _number(row.get("candidate_rank")) == 1
            ]
        if pass_rows:
            pass_count += 1

        by_direction = {str(row["direction"]): row for row in selected}
        first = selected[0] if selected else mutation_rows[0]
        item: dict[str, Any] = {
            "mutation": mutation,
            "status": "PASS" if pass_rows else "NO_PASS",
            "wildtype_codon": first.get("wildtype_codon"),
            "replacement_codon": first.get("replacement_codon"),
            "violations": first.get("violations") or None,
            "forward": None,
            "reverse": None,
        }
        for direction, key in (("F", "forward"), ("R", "reverse")):
            row = by_direction.get(direction)
            if row:
                item[key] = {
                    "name": row.get("primer_name"),
                    "sequence_5to3": row.get("sequence_5to3"),
                    "length": _number(row.get("length")),
                    "tm_min_c": _number(row.get("tm_min_c")),
                    "tm_max_c": _number(row.get("tm_max_c")),
                    "gc_min_percent": _number(row.get("gc_min_percent")),
                    "gc_max_percent": _number(row.get("gc_max_percent")),
                }
        mutations.append(item)

    return {
        "file": str(path.resolve()),
        "row_count": len(rows),
        "mutation_count": len(grouped),
        "pass_count": pass_count,
        "all_succeeded": pass_count == len(grouped) and bool(grouped),
        "mutations": mutations,
    }


def main() -> int:
    """解析参数、打印 JSON 汇总并返回适合自动化判断的退出码。"""

    parser = argparse.ArgumentParser(description="汇总 primer_designer 输出中的首选引物对")
    parser.add_argument("result", type=Path, help="primer_designer 生成的 .csv 或 .xlsx 文件")
    parser.add_argument(
        "--fallback-near-miss",
        action="store_true",
        help="没有 PASS 时展示排名第一的 NEAR_MISS 序列",
    )
    args = parser.parse_args()

    if not args.result.is_file():
        parser.error(f"结果文件不存在：{args.result}")
    try:
        summary = summarize(args.result, fallback_near_miss=args.fallback_near_miss)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
