#!/usr/bin/env python3
"""预览并规范化 EVOLVEpro 实验轮次表。"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

try:
    import pandas as pd
except ModuleNotFoundError as exc:
    raise SystemExit(
        "当前隔离运行环境缺少 pandas；请按 Skill 的 requirements.txt 准备依赖后重试。"
    ) from exc


AMINO_ACIDS = set("ACDEFGHIKLMNPQRSTVWY")
VARIANT_ALIASES = {"variant", "mutation", "mutant", "substitution", "突变", "突变体"}
ACTIVITY_ALIASES = {
    "activity",
    "fitness",
    "score",
    "measurement",
    "readout",
    "value",
    "活性",
    "活性值",
}
WT_ALIASES = {"WT", "WILDTYPE", "WILDTYPESEQUENCE"}


class NormalizationError(ValueError):
    """表示无法在不猜测用户语义的前提下完成规范化。"""


def _normalized_column_name(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value).strip()).lower()


def _read_sequence(sequence: str | None, wt_fasta: str | None) -> str:
    if bool(sequence) == bool(wt_fasta):
        raise NormalizationError("必须且只能提供 --sequence 或 --wt-fasta 之一。")

    if wt_fasta:
        path = Path(wt_fasta).expanduser()
        if not path.is_file():
            raise NormalizationError(f"WT FASTA 不存在: {path}")
        records: list[str] = []
        current: list[str] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if current:
                    records.append("".join(current))
                    current = []
                continue
            current.append(line)
        if current:
            records.append("".join(current))
        if len(records) != 1:
            raise NormalizationError(f"WT FASTA 必须且只能包含一个 record，当前为 {len(records)} 个。")
        sequence = records[0]

    cleaned = re.sub(r"\s+", "", str(sequence)).upper()
    invalid = sorted(set(cleaned) - AMINO_ACIDS)
    if not cleaned:
        raise NormalizationError("WT 序列不能为空。")
    if invalid:
        raise NormalizationError(f"WT 序列包含非标准氨基酸字符: {', '.join(invalid)}")
    return cleaned


def _read_table(path: Path, sheet: str | int | None) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".tsv":
        return pd.read_csv(path, sep="\t")
    if suffix in {".xlsx", ".xls"}:
        try:
            return pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
        except ImportError as exc:
            dependency = "openpyxl" if suffix == ".xlsx" else "xlrd"
            guidance = (
                "请按 Skill 的 requirements.txt 准备依赖后重试。"
                if suffix == ".xlsx"
                else "请将旧版 .xls 另存为 .xlsx 或 CSV 后重试。"
            )
            raise NormalizationError(
                f"当前隔离运行环境读取 {suffix} 需要 {dependency}；"
                f"{guidance}"
            ) from exc
    raise NormalizationError("输入文件只支持 .csv、.tsv、.xlsx 或 .xls。")


def _select_column(
    columns: list[Any],
    canonical: str,
    aliases: set[str],
    explicit: str | None,
) -> str:
    text_columns = [str(column) for column in columns]
    if explicit:
        if explicit not in text_columns:
            raise NormalizationError(f"指定列 {explicit!r} 不存在。可用列: {text_columns}")
        return explicit

    exact = [column for column in text_columns if column.strip().lower() == canonical.lower()]
    if len(exact) == 1:
        return exact[0]
    candidates = [
        column
        for column in text_columns
        if _normalized_column_name(column) in {_normalized_column_name(alias) for alias in aliases}
    ]
    if not candidates:
        raise NormalizationError(f"找不到 {canonical!r} 列。请使用 --{canonical.lower()}-column 明确指定。")
    if len(candidates) > 1:
        raise NormalizationError(
            f"{canonical!r} 列存在歧义: {candidates}。请使用 --{canonical.lower()}-column 明确指定。"
        )
    return candidates[0]


def _compact_variant(value: Any) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", "", str(value)).upper()


def _is_wt(value: str) -> bool:
    return re.sub(r"[\s_\-]+", "", value).upper() in WT_ALIASES


def _parse_single_component(value: str, sequence: str) -> tuple[int, str, str]:
    compact = _compact_variant(value)
    short_match = re.fullmatch(r"(\d+)([A-Z])", compact)
    full_match = re.fullmatch(r"([A-Z])(\d+)(?:->|>|/|_)?([A-Z])", compact)

    if short_match:
        position = int(short_match.group(1))
        mutant_aa = short_match.group(2)
        supplied_wt = None
    elif full_match:
        supplied_wt = full_match.group(1)
        position = int(full_match.group(2))
        mutant_aa = full_match.group(3)
    else:
        raise NormalizationError(f"突变 {value!r} 不是合法的单点突变格式。")

    if not 1 <= position <= len(sequence):
        raise NormalizationError(f"突变 {value!r} 的位置 {position} 超出 WT 长度 {len(sequence)}。")
    if mutant_aa not in AMINO_ACIDS:
        raise NormalizationError(f"突变 {value!r} 包含非标准目标氨基酸 {mutant_aa!r}。")

    wt_aa = sequence[position - 1]
    if supplied_wt and supplied_wt != wt_aa:
        raise NormalizationError(
            f"突变 {value!r} 标注的 WT 氨基酸为 {supplied_wt}，但序列第 {position} 位是 {wt_aa}。"
        )
    if mutant_aa == wt_aa:
        raise NormalizationError(f"突变 {value!r} 未改变第 {position} 位氨基酸。")
    return position, wt_aa, mutant_aa


def _normalize_variant(value: Any, sequence: str, mode: str) -> str:
    compact = _compact_variant(value)
    if not compact:
        raise NormalizationError("突变标识为空。")
    if _is_wt(compact):
        return "WT"
    if mode == "single":
        position, _, mutant_aa = _parse_single_component(compact, sequence)
        return f"{position}{mutant_aa}"

    components = [item for item in re.split(r"[_;,]+", compact) if item]
    normalized: list[str] = []
    positions: set[int] = set()
    for component in components:
        position, wt_aa, mutant_aa = _parse_single_component(component, sequence)
        if position in positions:
            raise NormalizationError(f"多突变 {value!r} 重复修改了位置 {position}。")
        positions.add(position)
        normalized.append(f"{wt_aa}{position}{mutant_aa}")
    return "_".join(normalized)


def _json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def preview_round_data(
    input_path: str | Path,
    *,
    sheet: str | int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise NormalizationError(f"输入文件不存在: {source}")
    dataframe = _read_table(source, sheet)
    dataframe = dataframe.dropna(how="all")
    preview_rows = [
        [_json_value(value) for value in row]
        for row in dataframe.head(max(1, min(int(limit), 50))).itertuples(index=False, name=None)
    ]
    return {
        "status": "ok",
        "mode": "preview",
        "input_path": str(source),
        "sheet": sheet if sheet is not None else 0,
        "columns": [str(column) for column in dataframe.columns],
        "row_count": len(dataframe),
        "preview_rows": preview_rows,
    }


def normalize_round_data(
    input_path: str | Path,
    output_path: str | Path,
    *,
    mode: str,
    sequence: str | None = None,
    wt_fasta: str | None = None,
    variant_column: str | None = None,
    activity_column: str | None = None,
    sheet: str | int | None = None,
) -> dict[str, Any]:
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise NormalizationError(f"输入文件不存在: {source}")
    if source == output:
        raise NormalizationError("输出路径不能与原始输入文件相同。")
    if mode not in {"single", "multi"}:
        raise NormalizationError("mode 只能是 single 或 multi。")

    wt_sequence = _read_sequence(sequence, wt_fasta)
    dataframe = _read_table(source, sheet)
    original_rows = len(dataframe)
    dataframe = dataframe.dropna(how="all").copy()
    if dataframe.empty:
        raise NormalizationError("实验轮次文件没有有效数据行。")

    variant_source = _select_column(list(dataframe.columns), "Variant", VARIANT_ALIASES, variant_column)
    activity_source = _select_column(list(dataframe.columns), "activity", ACTIVITY_ALIASES, activity_column)
    if variant_source == activity_source:
        raise NormalizationError("突变列和活性列不能是同一列。")

    errors: list[str] = []
    normalized_variants: list[str | None] = []
    changed_variants = 0
    for row_index, value in dataframe[variant_source].items():
        try:
            normalized = _normalize_variant(value, wt_sequence, mode)
            normalized_variants.append(normalized)
            if normalized != _compact_variant(value):
                changed_variants += 1
        except NormalizationError as exc:
            normalized_variants.append(None)
            errors.append(f"第 {row_index + 2} 行: {exc}")

    numeric_activity = pd.to_numeric(dataframe[activity_source], errors="coerce")
    for row_index, value in numeric_activity.items():
        if pd.isna(value) or not math.isfinite(float(value)):
            errors.append(f"第 {row_index + 2} 行: activity 必须是有限数值。")
    if errors:
        preview = errors[:20]
        if len(errors) > len(preview):
            preview.append(f"另有 {len(errors) - len(preview)} 个错误未显示。")
        raise NormalizationError("\n".join(preview))

    dataframe = dataframe.rename(columns={variant_source: "Variant", activity_source: "activity"})
    dataframe["Variant"] = normalized_variants
    dataframe["activity"] = numeric_activity.astype(float)
    canonical_order = ["Variant", "activity"]
    dataframe = dataframe[canonical_order + [column for column in dataframe.columns if column not in canonical_order]]
    duplicates = dataframe[dataframe["Variant"].duplicated(keep=False)]["Variant"].tolist()
    if duplicates:
        raise NormalizationError(f"规范化后存在重复突变: {sorted(set(duplicates))}")

    warnings: list[str] = []
    if "WT" not in set(dataframe["Variant"]):
        warnings.append("文件中没有 WT 行；请确认该轮次是否应包含 WT 测量值。")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".xlsx":
        dataframe.to_excel(output, index=False, engine="openpyxl")
    elif output.suffix.lower() == ".csv":
        dataframe.to_csv(output, index=False)
    else:
        raise NormalizationError("输出文件只支持 .xlsx 或 .csv。")
    return {
        "status": "ok",
        "mode": mode,
        "input_path": str(source),
        "output_path": str(output),
        "row_count": len(dataframe),
        "dropped_blank_rows": original_rows - len(dataframe),
        "changed_variants": changed_variants,
        "column_mapping": {variant_source: "Variant", activity_source: "activity"},
        "warnings": warnings,
    }


def _parse_sheet(value: str | None) -> str | int | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="用户上传的 CSV、TSV 或 Excel 文件")
    parser.add_argument("--preview", action="store_true", help="只输出表头和前几行，不规范化文件")
    parser.add_argument("--preview-limit", type=int, default=10, help="预览行数，最大 50")
    parser.add_argument("--output", help="新建的 .xlsx 或 .csv 输出路径")
    parser.add_argument("--mode", choices=["single", "multi"])
    wt_group = parser.add_mutually_exclusive_group()
    wt_group.add_argument("--sequence", help="WT 蛋白质序列")
    wt_group.add_argument("--wt-fasta", help="只包含一个 record 的 WT FASTA")
    parser.add_argument("--variant-column", help="明确指定原始突变列")
    parser.add_argument("--activity-column", help="明确指定原始活性列")
    parser.add_argument("--sheet", help="Excel 工作表名称或从 0 开始的序号")
    parser.add_argument("--report", help="可选 JSON 报告路径；规范化时默认写到输出文件旁")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    output = Path(args.output).expanduser() if args.output else None
    report_path = Path(args.report).expanduser() if args.report else (
        output.with_suffix(".report.json") if output is not None and not args.preview else None
    )
    try:
        if args.preview:
            report = preview_round_data(args.input, sheet=_parse_sheet(args.sheet), limit=args.preview_limit)
        else:
            missing = []
            if not args.output:
                missing.append("--output")
            if not args.mode:
                missing.append("--mode")
            if not args.sequence and not args.wt_fasta:
                missing.append("--sequence 或 --wt-fasta")
            if missing:
                raise NormalizationError(f"规范化模式缺少参数: {', '.join(missing)}")
            report = normalize_round_data(
                args.input,
                args.output,
                mode=args.mode,
                sequence=args.sequence,
                wt_fasta=args.wt_fasta,
                variant_column=args.variant_column,
                activity_column=args.activity_column,
                sheet=_parse_sheet(args.sheet),
            )
        exit_code = 0
    except Exception as exc:
        report = {
            "status": "error",
            "input_path": str(Path(args.input).expanduser()),
            "output_path": "",
            "errors": str(exc).splitlines(),
        }
        exit_code = 1

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
