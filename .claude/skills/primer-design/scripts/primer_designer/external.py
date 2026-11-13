"""对网站或其他工具生成的外部引物进行回评和候选池排名。"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .codons import translate_codon
from .designer import PrimerDesigner
from .exceptions import ExternalPrimerValidationError, MutationValidationError
from .models import (
    MutationSpec,
    MutationType,
    PrimerDesignConfig,
    PrimerMetrics,
    PrimerPair,
    QuikChangeMetrics,
)
from .sequence import normalize_dna, validate_cds
from .thermodynamics import IUPAC, reverse_complement


@dataclass(frozen=True)
class ExternalPrimerPair:
    """一条外部记录；反向引物省略时按正向引物自动生成。"""

    mutation: str | MutationSpec
    forward_sequence: str
    reverse_sequence: str | None = None
    forward_name: str | None = None
    reverse_name: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class ExternalPrimerEvaluation:
    """外部引物的约束指标、兼容性和在当前候选池中的排名。"""

    mutation: MutationSpec
    preset: str
    forward_sequence: str
    reverse_sequence: str
    forward_name: str
    reverse_name: str
    source: str | None
    reverse_auto_generated: bool
    reverse_complement_match: bool
    strategy_compatible: bool
    evaluation_status: str
    constraint_status: str
    wildtype_codon: str
    replacement_codon: str | None
    left_flank_length: int | None
    right_flank_length: int | None
    alignment_count: int
    forward_metrics: PrimerMetrics
    reverse_metrics: PrimerMetrics
    quikchange_metrics: QuikChangeMetrics | None
    score: float | None
    violations: tuple[str, ...]
    advisories: tuple[str, ...]
    generated_by_algorithm: bool
    ranking_type: str | None
    comparison_scope: str | None
    overall_rank: int | None
    comparison_total: int | None
    pass_rank: int | None
    pass_total: int | None
    near_miss_rank: int | None
    near_miss_total: int | None
    warnings: tuple[str, ...] = ()


EXTERNAL_INPUT_ALIASES: dict[str, tuple[str, ...]] = {
    "mutation": ("mutation", "变异", "突变"),
    "forward_sequence": (
        "forward_sequence", "forward", "f_sequence", "正向引物", "正向序列"
    ),
    "reverse_sequence": (
        "reverse_sequence", "reverse", "r_sequence", "反向引物", "反向序列"
    ),
    "forward_name": ("forward_name", "f_name", "正向引物名称"),
    "reverse_name": ("reverse_name", "r_name", "反向引物名称"),
    "source": ("source", "来源"),
}


EXTERNAL_EVALUATION_FIELDS = [
    "mutation",
    "preset",
    "source",
    "forward_name",
    "reverse_name",
    "forward_sequence",
    "reverse_sequence",
    "reverse_auto_generated",
    "reverse_complement_match",
    "strategy_compatible",
    "evaluation_status",
    "constraint_status",
    "wildtype_codon",
    "replacement_codon",
    "left_flank_length",
    "right_flank_length",
    "alignment_count",
    "generated_by_algorithm",
    "ranking_type",
    "comparison_scope",
    "overall_rank",
    "comparison_total",
    "pass_rank",
    "pass_total",
    "near_miss_rank",
    "near_miss_total",
    "pair_score",
    "violations",
    "advisories",
    "warnings",
    "quikchange_tm_min_c",
    "quikchange_tm_max_c",
    "quikchange_mismatch_min_percent",
    "quikchange_mismatch_max_percent",
    "quikchange_estimated_energy_cost_min_percent",
    "quikchange_estimated_energy_cost_max_percent",
    "forward_length",
    "forward_tm_min_c",
    "forward_tm_max_c",
    "forward_gc_min_percent",
    "forward_gc_max_percent",
    "forward_hairpin_tm_max_c",
    "forward_homodimer_tm_max_c",
    "forward_variant_count",
    "reverse_length",
    "reverse_tm_min_c",
    "reverse_tm_max_c",
    "reverse_gc_min_percent",
    "reverse_gc_max_percent",
    "reverse_hairpin_tm_max_c",
    "reverse_homodimer_tm_max_c",
    "reverse_variant_count",
]


def _normalize_primer(sequence: str, label: str) -> str:
    """清理外部引物中的空白，并校验 IUPAC DNA 字符。"""

    normalized = re.sub(r"\s+", "", sequence or "").upper()
    if not normalized:
        raise ExternalPrimerValidationError(f"{label}为空")
    invalid = sorted(set(normalized) - set(IUPAC))
    if invalid:
        raise ExternalPrimerValidationError(
            f"{label}只能包含合法 IUPAC DNA 碱基，发现：{''.join(invalid)}"
        )
    return normalized


def _lookup(row: dict[str, Any], field: str) -> str | None:
    """按英文或中文列别名读取并清理一个单元格。"""

    normalized = {
        str(key).strip().lower(): value
        for key, value in row.items()
        if key is not None
    }
    for alias in EXTERNAL_INPUT_ALIASES[field]:
        value = normalized.get(alias.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _row_to_external(row: dict[str, Any], row_number: int) -> ExternalPrimerPair:
    """把一行表格转换为外部引物记录，并报告可定位的行号错误。"""

    mutation = _lookup(row, "mutation")
    forward = _lookup(row, "forward_sequence")
    if not mutation or not forward:
        raise ExternalPrimerValidationError(
            f"外部引物第 {row_number} 行缺少 mutation 或 forward_sequence"
        )
    return ExternalPrimerPair(
        mutation=mutation,
        forward_sequence=forward,
        reverse_sequence=_lookup(row, "reverse_sequence"),
        forward_name=_lookup(row, "forward_name"),
        reverse_name=_lookup(row, "reverse_name"),
        source=_lookup(row, "source"),
    )


def read_external_primers(path: str | Path) -> list[ExternalPrimerPair]:
    """读取一行一对引物的 CSV/XLSX 文件。"""

    source = Path(path)
    if source.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(source, read_only=True, data_only=True)
        sheet = (
            workbook["website_primers"]
            if "website_primers" in workbook.sheetnames
            else workbook.active
        )
        values = sheet.iter_rows(values_only=True)
        try:
            headers = [str(value).strip() if value is not None else "" for value in next(values)]
        except StopIteration:
            return []
        rows = [dict(zip(headers, values_row)) for values_row in values]
    else:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    result = [
        _row_to_external(row, row_number)
        for row_number, row in enumerate(rows, start=2)
        if any(value is not None and str(value).strip() for value in row.values())
    ]
    if not result:
        raise ExternalPrimerValidationError("外部引物文件中没有数据行")
    return result


class ExternalPrimerEvaluator:
    """使用现有候选生成、约束和评分规则评价外部引物。"""

    def __init__(self, config: PrimerDesignConfig | None = None):
        self.config = config or PrimerDesignConfig()
        self.designer = PrimerDesigner(self.config)

    def evaluate(
        self,
        sequence: str,
        primer_pairs: Iterable[ExternalPrimerPair],
    ) -> tuple[ExternalPrimerEvaluation, ...]:
        """在同一母本序列上批量评价外部引物。"""

        template = normalize_dna(sequence)
        protein = validate_cds(template, self.config)
        pairs = tuple(primer_pairs)
        if not pairs:
            raise ExternalPrimerValidationError("外部引物清单为空")
        return tuple(self._evaluate_one(template, protein, item) for item in pairs)

    def _evaluate_one(
        self,
        template: str,
        protein: str,
        external: ExternalPrimerPair,
    ) -> ExternalPrimerEvaluation:
        """校验一个位点，计算外部引物指标并插入同密码子候选池排名。"""

        mutation = (
            MutationSpec.parse(external.mutation)
            if isinstance(external.mutation, str)
            else external.mutation
        )
        codon_start, wildtype_codon = self._mutation_context(
            template, protein, mutation
        )
        forward = _normalize_primer(external.forward_sequence, f"{mutation.name} 正向引物")
        reverse_auto = not external.reverse_sequence
        reverse = (
            reverse_complement(forward)
            if reverse_auto
            else _normalize_primer(external.reverse_sequence or "", f"{mutation.name} 反向引物")
        )
        reverse_matches = reverse_complement(forward) == reverse
        forward_metrics = self.designer.thermo.analyze(forward)
        reverse_metrics = self.designer.thermo.analyze(reverse)
        alignments = self._find_alignments(
            template, mutation, codon_start, forward
        )
        warnings: list[str] = []
        if len(alignments) > 1:
            warnings.append(
                f"正向引物存在 {len(alignments)} 个可行突变密码子位置，已选择最接近中央者"
            )
        if not reverse_matches:
            warnings.append("反向引物不是正向引物的完全反向互补，与当前策略不完全兼容")

        base = dict(
            mutation=mutation,
            preset=self.config.preset,
            forward_sequence=forward,
            reverse_sequence=reverse,
            forward_name=external.forward_name or f"{mutation.name}-external-F",
            reverse_name=external.reverse_name or f"{mutation.name}-external-R",
            source=external.source,
            reverse_auto_generated=reverse_auto,
            reverse_complement_match=reverse_matches,
            strategy_compatible=reverse_matches and bool(alignments),
            wildtype_codon=wildtype_codon,
            alignment_count=len(alignments),
            forward_metrics=forward_metrics,
            reverse_metrics=reverse_metrics,
        )
        if not alignments:
            warnings.append("正向引物无法按指定突变位点对齐母本，不能计算候选池排名")
            # 通用模式仍可仅依据引物自身指标报告约束状态；QuikChange
            # 公式依赖母本错配位置，因此未对齐时不能完成协议判定。
            violations = tuple(
                self.designer._violations(forward_metrics, reverse_metrics)
                if not self.designer.uses_quikchange
                else ("无法对齐，不能计算 QuikChange Tm",)
            )
            return ExternalPrimerEvaluation(
                **base,
                evaluation_status="UNALIGNED",
                constraint_status="UNCHECKED" if self.designer.uses_quikchange else (
                    "PASS" if not violations else "NEAR_MISS"
                ),
                replacement_codon=None,
                left_flank_length=None,
                right_flank_length=None,
                quikchange_metrics=None,
                score=None,
                violations=violations,
                advisories=(),
                generated_by_algorithm=False,
                ranking_type=None,
                comparison_scope=None,
                overall_rank=None,
                comparison_total=None,
                pass_rank=None,
                pass_total=None,
                near_miss_rank=None,
                near_miss_total=None,
                warnings=tuple(warnings),
            )

        left_length, right_length, replacement_codon = alignments[0]
        start = codon_start - left_length
        end = codon_start + 3 + right_length
        quikchange_metrics = None
        if self.designer.uses_quikchange:
            quikchange_metrics = self.designer.quikchange.analyze(
                forward,
                template[start:end],
                left_length,
            )
        violations = tuple(
            self.designer._violations(
                forward_metrics,
                reverse_metrics,
                left_flank=left_length,
                right_flank=right_length,
                quikchange_metrics=quikchange_metrics,
            )
        )
        if not reverse_matches:
            violations += ("反向引物不是正向引物的完全反向互补",)
        advisories = tuple(
            self.designer._advisories(
                forward,
                reverse,
                forward_metrics,
                reverse_metrics,
            )
        )
        score = self.designer._score(
            forward_metrics,
            reverse_metrics,
            left_length,
            right_length,
            list(violations),
            quikchange_metrics=quikchange_metrics,
            advisories=advisories,
        )
        candidates = self.designer._enumerate_candidates(
            template,
            mutation,
            codon_start,
            wildtype_codon,
            replacement_codon,
        )
        candidates.sort(key=self._pair_key)
        exact_index = next(
            (
                index
                for index, pair in enumerate(candidates)
                if pair.forward.sequence == forward and pair.reverse.sequence == reverse
            ),
            None,
        )
        valid = not violations
        external_key = (not valid, score, forward)
        generated = exact_index is not None
        overall_rank = (
            exact_index + 1
            if exact_index is not None
            else 1 + sum(self._pair_key(pair) < external_key for pair in candidates)
        )
        comparison_total = len(candidates) + (0 if generated else 1)
        valid_candidates = [pair for pair in candidates if pair.valid]
        invalid_candidates = [pair for pair in candidates if not pair.valid]
        pass_rank = None
        near_miss_rank = None
        if valid:
            pass_rank = self._category_rank(
                valid_candidates, forward, reverse, external_key
            )
        else:
            near_miss_rank = self._category_rank(
                invalid_candidates, forward, reverse, external_key
            )

        return ExternalPrimerEvaluation(
            **base,
            evaluation_status="RANKED",
            constraint_status="PASS" if valid else "NEAR_MISS",
            replacement_codon=replacement_codon,
            left_flank_length=left_length,
            right_flank_length=right_length,
            quikchange_metrics=quikchange_metrics,
            score=score,
            violations=violations,
            advisories=advisories,
            generated_by_algorithm=generated,
            ranking_type="generated" if generated else "inserted",
            comparison_scope="same_replacement_codon",
            overall_rank=overall_rank,
            comparison_total=comparison_total,
            pass_rank=pass_rank,
            pass_total=len(valid_candidates) + (1 if valid and not generated else 0),
            near_miss_rank=near_miss_rank,
            near_miss_total=len(invalid_candidates) + (1 if not valid and not generated else 0),
            warnings=tuple(warnings),
        )

    def _mutation_context(
        self, template: str, protein: str, mutation: MutationSpec
    ) -> tuple[int, str]:
        """校验外部记录的氨基酸坐标和母本氨基酸。"""

        if mutation.position < 1 or mutation.position > len(template) // 3:
            raise MutationValidationError(
                f"{mutation.name}：位置 {mutation.position} 超出 CDS 长度 {len(template) // 3}"
            )
        codon_start = (mutation.position - 1) * 3
        wildtype_codon = template[codon_start:codon_start + 3]
        observed = translate_codon(wildtype_codon)
        if mutation.reference_aa and mutation.reference_aa != observed:
            raise MutationValidationError(
                f"{mutation.name}：母本位置 {mutation.position} 为 {observed}"
                f"（{wildtype_codon}），不是 {mutation.reference_aa}"
            )
        if protein and protein[mutation.position - 1] != observed:
            raise MutationValidationError(f"{mutation.name}：CDS 翻译结果与密码子不一致")
        return codon_start, wildtype_codon

    @staticmethod
    def _codon_matches_mutation(codon: str, mutation: MutationSpec) -> bool:
        """判断外部引物中央三碱基是否符合突变描述。"""

        if mutation.mutation_type is MutationType.DEGENERATE:
            return codon == (mutation.degenerate_codon or "NNK").upper()
        if set(codon) - set("ACGT"):
            return False
        if mutation.replacement_codon and codon != mutation.replacement_codon:
            return False
        return translate_codon(codon) == mutation.target_aa

    def _find_alignments(
        self,
        template: str,
        mutation: MutationSpec,
        codon_start: int,
        forward: str,
    ) -> list[tuple[int, int, str]]:
        """枚举正向引物中可能的突变密码子位置并比对两侧母本序列。"""

        alignments: list[tuple[int, int, str]] = []
        for left_length in range(1, len(forward) - 3):
            right_length = len(forward) - 3 - left_length
            if right_length < 1:
                continue
            start = codon_start - left_length
            end = codon_start + 3 + right_length
            if start < 0 or end > len(template):
                continue
            codon = forward[left_length:left_length + 3]
            if not self._codon_matches_mutation(codon, mutation):
                continue
            if forward[:left_length] != template[start:codon_start]:
                continue
            if forward[left_length + 3:] != template[codon_start + 3:end]:
                continue
            alignments.append((left_length, right_length, codon))
        alignments.sort(key=lambda item: (abs(item[0] - item[1]), item[0]))
        return alignments

    @staticmethod
    def _pair_key(pair: PrimerPair) -> tuple[bool, float, str]:
        """复用设计器的稳定排序键。"""

        return not pair.valid, pair.score, pair.forward.sequence

    def _category_rank(
        self,
        candidates: list[PrimerPair],
        forward: str,
        reverse: str,
        external_key: tuple[bool, float, str],
    ) -> int:
        """计算外部候选在 PASS 或 NEAR_MISS 子集合中的排名。"""

        exact_index = next(
            (
                index
                for index, pair in enumerate(candidates)
                if pair.forward.sequence == forward and pair.reverse.sequence == reverse
            ),
            None,
        )
        if exact_index is not None:
            return exact_index + 1
        return 1 + sum(self._pair_key(pair) < external_key for pair in candidates)


def evaluation_row(item: ExternalPrimerEvaluation) -> dict[str, Any]:
    """把外部评价结果转换为一行可导出的扁平字段。"""

    forward = item.forward_metrics
    reverse = item.reverse_metrics
    return {
        "mutation": item.mutation.name,
        "preset": item.preset,
        "source": item.source or "",
        "forward_name": item.forward_name,
        "reverse_name": item.reverse_name,
        "forward_sequence": item.forward_sequence,
        "reverse_sequence": item.reverse_sequence,
        "reverse_auto_generated": item.reverse_auto_generated,
        "reverse_complement_match": item.reverse_complement_match,
        "strategy_compatible": item.strategy_compatible,
        "evaluation_status": item.evaluation_status,
        "constraint_status": item.constraint_status,
        "wildtype_codon": item.wildtype_codon,
        "replacement_codon": item.replacement_codon or "",
        "left_flank_length": item.left_flank_length,
        "right_flank_length": item.right_flank_length,
        "alignment_count": item.alignment_count,
        "generated_by_algorithm": item.generated_by_algorithm,
        "ranking_type": item.ranking_type or "",
        "comparison_scope": item.comparison_scope or "",
        "overall_rank": item.overall_rank,
        "comparison_total": item.comparison_total,
        "pass_rank": item.pass_rank,
        "pass_total": item.pass_total,
        "near_miss_rank": item.near_miss_rank,
        "near_miss_total": item.near_miss_total,
        "pair_score": round(item.score, 4) if item.score is not None else None,
        "violations": "; ".join(item.violations),
        "advisories": "; ".join(item.advisories),
        "warnings": "; ".join(item.warnings),
        "quikchange_tm_min_c": (
            round(item.quikchange_metrics.tm_min, 2)
            if item.quikchange_metrics else None
        ),
        "quikchange_tm_max_c": (
            round(item.quikchange_metrics.tm_max, 2)
            if item.quikchange_metrics else None
        ),
        "quikchange_mismatch_min_percent": (
            round(item.quikchange_metrics.mismatch_percent_min, 2)
            if item.quikchange_metrics else None
        ),
        "quikchange_mismatch_max_percent": (
            round(item.quikchange_metrics.mismatch_percent_max, 2)
            if item.quikchange_metrics else None
        ),
        "quikchange_estimated_energy_cost_min_percent": (
            round(item.quikchange_metrics.estimated_energy_cost_min, 2)
            if item.quikchange_metrics else None
        ),
        "quikchange_estimated_energy_cost_max_percent": (
            round(item.quikchange_metrics.estimated_energy_cost_max, 2)
            if item.quikchange_metrics else None
        ),
        "forward_length": forward.length,
        "forward_tm_min_c": round(forward.tm_min, 2),
        "forward_tm_max_c": round(forward.tm_max, 2),
        "forward_gc_min_percent": round(forward.gc_min, 2),
        "forward_gc_max_percent": round(forward.gc_max, 2),
        "forward_hairpin_tm_max_c": round(forward.hairpin_tm_max, 2),
        "forward_homodimer_tm_max_c": round(forward.homodimer_tm_max, 2),
        "forward_variant_count": forward.variant_count,
        "reverse_length": reverse.length,
        "reverse_tm_min_c": round(reverse.tm_min, 2),
        "reverse_tm_max_c": round(reverse.tm_max, 2),
        "reverse_gc_min_percent": round(reverse.gc_min, 2),
        "reverse_gc_max_percent": round(reverse.gc_max, 2),
        "reverse_hairpin_tm_max_c": round(reverse.hairpin_tm_max, 2),
        "reverse_homodimer_tm_max_c": round(reverse.homodimer_tm_max, 2),
        "reverse_variant_count": reverse.variant_count,
    }


def export_external_evaluations(
    evaluations: Iterable[ExternalPrimerEvaluation], path: str | Path
) -> Path:
    """把一行一对的外部引物评价结果导出为 CSV 或 XLSX。"""

    rows = [evaluation_row(item) for item in evaluations]
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() != ".xlsx":
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXTERNAL_EVALUATION_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return output

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "external_primer_ranking"
    sheet.append(EXTERNAL_EVALUATION_FIELDS)
    for row in rows:
        sheet.append([row[field] for field in EXTERNAL_EVALUATION_FIELDS])
    fill = PatternFill("solid", fgColor="D9EAD3")
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    from openpyxl.utils import get_column_letter

    wide_fields = {
        "forward_sequence": 58,
        "reverse_sequence": 58,
        "violations": 42,
        "advisories": 46,
        "warnings": 46,
        "preset": 20,
    }
    for index, field in enumerate(EXTERNAL_EVALUATION_FIELDS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = wide_fields.get(
            field, 18
        )
    workbook.save(output)
    return output


def evaluate_external_primers(
    sequence: str,
    primer_pairs: Iterable[ExternalPrimerPair],
    config: PrimerDesignConfig | None = None,
) -> tuple[ExternalPrimerEvaluation, ...]:
    """无需手动实例化评价器的便捷 API。"""

    return ExternalPrimerEvaluator(config).evaluate(sequence, primer_pairs)
