"""把设计结果转换为表格行，并导出为 CSV 或 Excel。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .models import BatchDesignResult, PrimerPair


# CSV 和 Excel 共用同一套字段顺序，便于 Skill 或下游脚本稳定读取。
FIELDNAMES = [
    "mutation",
    "preset",
    "mutation_type",
    "wildtype_codon",
    "replacement_codon",
    "candidate_rank",
    "primer_name",
    "direction",
    "sequence_5to3",
    "length",
    "tm_min_c",
    "tm_max_c",
    "gc_min_percent",
    "gc_max_percent",
    "hairpin_tm_max_c",
    "homodimer_tm_max_c",
    "variant_count",
    "left_flank_length",
    "right_flank_length",
    "pair_score",
    "status",
    "violations",
    "advisories",
    "quikchange_tm_min_c",
    "quikchange_tm_max_c",
    "quikchange_mismatch_min_percent",
    "quikchange_mismatch_max_percent",
    "quikchange_estimated_energy_cost_min_percent",
    "quikchange_estimated_energy_cost_max_percent",
]


def _pair_rows(pair: PrimerPair, preset: str) -> list[dict[str, Any]]:
    """把一对正反向引物拆成两条表格记录。"""

    rows = []
    for primer in (pair.forward, pair.reverse):
        metrics = primer.metrics
        rows.append(
            {
                "mutation": pair.mutation.name,
                "preset": preset,
                "mutation_type": pair.mutation.mutation_type.value,
                "wildtype_codon": pair.wildtype_codon,
                "replacement_codon": pair.replacement_codon,
                "candidate_rank": pair.candidate_rank,
                "primer_name": primer.name,
                "direction": primer.direction,
                "sequence_5to3": primer.sequence,
                "length": metrics.length,
                "tm_min_c": round(metrics.tm_min, 2),
                "tm_max_c": round(metrics.tm_max, 2),
                "gc_min_percent": round(metrics.gc_min, 2),
                "gc_max_percent": round(metrics.gc_max, 2),
                "hairpin_tm_max_c": round(metrics.hairpin_tm_max, 2),
                "homodimer_tm_max_c": round(metrics.homodimer_tm_max, 2),
                "variant_count": metrics.variant_count,
                "left_flank_length": pair.left_flank_length,
                "right_flank_length": pair.right_flank_length,
                "pair_score": round(pair.score, 4),
                "status": "PASS" if pair.valid else "NEAR_MISS",
                "violations": "; ".join(pair.violations),
                "advisories": "; ".join(pair.advisories),
                "quikchange_tm_min_c": (
                    round(pair.quikchange_metrics.tm_min, 2)
                    if pair.quikchange_metrics else None
                ),
                "quikchange_tm_max_c": (
                    round(pair.quikchange_metrics.tm_max, 2)
                    if pair.quikchange_metrics else None
                ),
                "quikchange_mismatch_min_percent": (
                    round(pair.quikchange_metrics.mismatch_percent_min, 2)
                    if pair.quikchange_metrics else None
                ),
                "quikchange_mismatch_max_percent": (
                    round(pair.quikchange_metrics.mismatch_percent_max, 2)
                    if pair.quikchange_metrics else None
                ),
                "quikchange_estimated_energy_cost_min_percent": (
                    round(pair.quikchange_metrics.estimated_energy_cost_min, 2)
                    if pair.quikchange_metrics else None
                ),
                "quikchange_estimated_energy_cost_max_percent": (
                    round(pair.quikchange_metrics.estimated_energy_cost_max, 2)
                    if pair.quikchange_metrics else None
                ),
            }
        )
    return rows


def result_rows(
    result: BatchDesignResult, *, include_near_misses: bool = False
) -> list[dict[str, Any]]:
    """把批量结果转换成适合 JSON/表格输出的字典列表。"""

    rows: list[dict[str, Any]] = []
    for pair in result.iter_pairs(include_near_misses=include_near_misses):
        rows.extend(_pair_rows(pair, result.config.preset))
    return rows


def export_csv(
    result: BatchDesignResult,
    path: str | Path,
    *,
    include_near_misses: bool = False,
) -> Path:
    """导出带 UTF-8 BOM 的 CSV，确保常见中文版 Excel 可正确识别中文。"""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(result_rows(result, include_near_misses=include_near_misses))
    return output


def export_xlsx(
    result: BatchDesignResult,
    path: str | Path,
    *,
    include_near_misses: bool = False,
) -> Path:
    """导出带表头样式、筛选器和冻结窗格的 Excel 文件。"""

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "primers"
    sheet.append(FIELDNAMES)
    for row in result_rows(result, include_near_misses=include_near_misses):
        sheet.append([row[field] for field in FIELDNAMES])
    # 简单设置表头样式，并冻结首行方便浏览大量引物。
    fill = PatternFill("solid", fgColor="D9EAD3")
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    # 按字段名设置宽度，增加字段后不依赖固定列字母。
    from openpyxl.utils import get_column_letter

    wide_fields = {
        "sequence_5to3": 58,
        "violations": 42,
        "advisories": 46,
        "preset": 20,
    }
    for index, field in enumerate(FIELDNAMES, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = wide_fields.get(
            field, 18
        )
    workbook.save(output)
    return output
