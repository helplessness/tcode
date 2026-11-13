"""突变引物候选生成、约束筛选和评分排序的核心流程。"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .codons import CODONS_BY_AA, minimal_change_codons, translate_codon
from .exceptions import MutationValidationError
from .models import (
    BatchDesignResult,
    DesignPreset,
    MutationDesignResult,
    MutationSpec,
    MutationType,
    Primer,
    PrimerDesignConfig,
    PrimerMetrics,
    PrimerPair,
    QuikChangeMetrics,
)
from .protocols import QuikChangeAnalyzer
from .sequence import normalize_dna, validate_cds
from .thermodynamics import IUPAC, ThermodynamicsAnalyzer, reverse_complement


class PrimerDesigner:
    """完全互补、突变位点居中的突变引物设计器。

    当前类实现 ``overlap_complementary`` 策略：正向引物包含突变密码子，
    反向引物是正向引物的完全反向互补。它不是 Q5 背靠背策略。
    """

    def __init__(self, config: PrimerDesignConfig | None = None):
        """保存设计参数，并创建共享的 Primer3 热力学计算器。"""

        self.config = config or PrimerDesignConfig()
        self.thermo = ThermodynamicsAnalyzer(self.config)
        self.quikchange = QuikChangeAnalyzer(self.config)

    @property
    def uses_quikchange(self) -> bool:
        """当前是否采用 QuikChange II 协议规则。"""

        return self.config.preset == DesignPreset.QUIKCHANGE_II.value

    def design(
        self,
        sequence: str,
        mutations: Iterable[str | MutationSpec],
    ) -> BatchDesignResult:
        """批量设计一个或多个突变位点的引物。

        ``sequence`` 可以是 FASTA 或纯 DNA 文本；``mutations`` 可以混合使用
        ``C231A`` 字符串和 ``MutationSpec`` 对象。
        """

        # 第一步：清洗 DNA，并按照配置验证完整 CDS。
        template = normalize_dna(sequence)
        protein = validate_cds(template, self.config)

        # 第二步：忽略空白项，将简写字符串转换为统一的 MutationSpec。
        cleaned = tuple(
            item for item in mutations
            if not isinstance(item, str) or item.strip()
        )
        parsed = tuple(
            MutationSpec.parse(item) if isinstance(item, str) else item
            for item in cleaned
        )
        if not parsed:
            raise MutationValidationError("突变清单为空")

        # 第三步：每个突变独立生成和筛选候选，最后合并为批量结果。
        results = tuple(
            self._design_one(template, protein, mutation) for mutation in parsed
        )
        return BatchDesignResult(sequence=template, results=results, config=self.config)

    def _design_one(
        self,
        template: str,
        protein: str,
        mutation: MutationSpec,
    ) -> MutationDesignResult:
        """完成单个突变位点的坐标校验、候选生成和排序。"""

        # 用户输入的是从 1 开始的氨基酸坐标。
        if mutation.position < 1:
            raise MutationValidationError(f"{mutation.name}：氨基酸位置必须从 1 开始")
        max_position = len(template) // 3
        if mutation.position > max_position:
            raise MutationValidationError(
                f"{mutation.name}：位置 {mutation.position} 超出 CDS 长度 {max_position}"
            )

        # Python 字符串从 0 开始，因此第 n 个氨基酸的密码子起点为 (n-1)*3。
        codon_start = (mutation.position - 1) * 3
        wildtype_codon = template[codon_start:codon_start + 3]
        observed_aa = translate_codon(wildtype_codon)

        # 如果突变写明了母本氨基酸，例如 C231A，就必须与序列翻译结果一致。
        if mutation.reference_aa and mutation.reference_aa != observed_aa:
            raise MutationValidationError(
                f"{mutation.name}：母本位置 {mutation.position} 为 {observed_aa}"
                f"（{wildtype_codon}），不是 {mutation.reference_aa}"
            )
        if protein and protein[mutation.position - 1] != observed_aa:
            raise MutationValidationError(f"{mutation.name}：CDS 翻译结果与密码子不一致")

        # 定点突变可能存在多个并列的最少改动密码子，因此逐个生成候选。
        replacement_codons = self._replacement_codons(mutation, wildtype_codon)
        all_pairs: list[PrimerPair] = []
        for replacement_codon in replacement_codons:
            all_pairs.extend(
                self._enumerate_candidates(
                    template,
                    mutation,
                    codon_start,
                    wildtype_codon,
                    replacement_codon,
                )
            )
        if not all_pairs:
            raise MutationValidationError(
                f"{mutation.name}：突变位点靠近序列边界，无法取得足够的引物侧翼"
            )

        # 排序优先级：合格候选优先，其次分数越低越好，最后用序列稳定打破平局。
        all_pairs.sort(key=lambda pair: (not pair.valid, pair.score, pair.forward.sequence))
        valid = [pair for pair in all_pairs if pair.valid]
        invalid = [pair for pair in all_pairs if not pair.valid]

        # 严格模式的主要结果只包含合格候选；宽松模式可返回最接近约束的候选。
        if self.config.strict:
            candidates = valid[:self.config.num_return]
        else:
            candidates = all_pairs[:self.config.num_return]
        near_misses = invalid[:self.config.near_miss_count]
        if not self.config.strict:
            selected_ids = {id(pair) for pair in candidates}
            near_misses = [pair for pair in near_misses if id(pair) not in selected_ids]

        # 排名后重新生成可读名称，例如 C231A-F、C231A-C2-F、C231A-NM1-F。
        ranked_candidates = tuple(
            self._assign_rank(pair, rank) for rank, pair in enumerate(candidates, start=1)
        )
        ranked_near_misses = tuple(
            self._assign_rank(pair, rank, near_miss=True)
            for rank, pair in enumerate(near_misses, start=1)
        )

        warnings = []
        if not valid:
            if self.uses_quikchange:
                warnings.append("没有候选同时满足 QuikChange II 的硬性约束")
            else:
                warnings.append("没有候选同时满足全部长度、Primer3 Tm 和 GC 约束")
        if mutation.mutation_type is MutationType.DEGENERATE:
            warnings.append("简并引物的 Tm、GC 和结构指标按所有具体变体的范围报告")
        return MutationDesignResult(
            mutation=mutation,
            candidates=ranked_candidates,
            near_misses=ranked_near_misses,
            warnings=tuple(warnings),
        )

    def _replacement_codons(
        self, mutation: MutationSpec, wildtype_codon: str
    ) -> tuple[str, ...]:
        """根据突变类型确定写入引物的三碱基密码子。"""

        if mutation.mutation_type is MutationType.DEGENERATE:
            # 简并突变直接使用 NNK、NNS 等 IUPAC 三碱基模式。
            pattern = (mutation.degenerate_codon or "NNK").upper()
            invalid = sorted(set(pattern) - set(IUPAC))
            if len(pattern) != 3 or invalid:
                raise MutationValidationError(
                    f"{mutation.name}：简并密码子必须为 3 个合法 IUPAC 碱基"
                )
            return (pattern,)

        # 定点突变先验证目标氨基酸，再处理用户指定或自动选择的密码子。
        target = (mutation.target_aa or "").upper()
        if target not in CODONS_BY_AA or target == "*":
            raise MutationValidationError(f"{mutation.name}：目标氨基酸无效：{target}")
        if mutation.replacement_codon:
            codon = mutation.replacement_codon.upper()
            if len(codon) != 3 or set(codon) - set("ACGT"):
                raise MutationValidationError(f"{mutation.name}：指定密码子必须为 3 个 A/C/G/T")
            if translate_codon(codon) != target:
                raise MutationValidationError(
                    f"{mutation.name}：指定密码子 {codon} 不编码 {target}"
                )
            return (codon,)
        # 未指定准确密码子时，选择相对母本密码子碱基改动最少的同义密码子。
        return minimal_change_codons(target, wildtype_codon)

    def _enumerate_candidates(
        self,
        template: str,
        mutation: MutationSpec,
        codon_start: int,
        wildtype_codon: str,
        replacement_codon: str,
    ) -> list[PrimerPair]:
        """枚举不同总长度和左右侧翼组合，构造完整候选引物对。"""

        # QuikChange II 把 45 bp 视为优选上限而不是硬上限，因此默认搜索到
        # absolute_max；通用模式继续沿用原来的长度范围与 near miss 扩展。
        if self.uses_quikchange:
            min_length = (
                self.config.min_length
                if self.config.strict and self.config.near_miss_count == 0
                else max(3, self.config.min_length - self.config.relax_length_by)
            )
            max_length = self.config.quikchange_absolute_max_length
        elif self.config.strict and self.config.near_miss_count == 0:
            min_length = self.config.min_length
            max_length = self.config.max_length
        else:
            min_length = max(3, self.config.min_length - self.config.relax_length_by)
            max_length = min(60, self.config.max_length + self.config.relax_length_by)

        candidates: list[PrimerPair] = []
        seen: set[str] = set()

        # 外层改变总长度，内层通过 delta 轻微移动突变密码子的位置。
        for length in range(min_length, max_length + 1):
            flank_total = length - 3
            centered_left = flank_total // 2
            for delta in range(-self.config.center_slack, self.config.center_slack + 1):
                left_length = centered_left + delta
                right_length = flank_total - left_length
                if left_length < 1 or right_length < 1:
                    continue
                # 计算候选在母本序列上的截取边界；越界候选直接跳过。
                start = codon_start - left_length
                end = codon_start + 3 + right_length
                if start < 0 or end > len(template):
                    continue
                # 正向引物由母本左侧翼、替换密码子和母本右侧翼组成。
                forward_sequence = (
                    template[start:codon_start]
                    + replacement_codon
                    + template[codon_start + 3:end]
                )
                if forward_sequence in seen:
                    continue
                seen.add(forward_sequence)
                # 当前策略要求反向引物与正向引物完全反向互补。
                reverse_sequence = reverse_complement(forward_sequence)

                # Primer3 始终负责通用 Tm、GC、发卡和同源二聚体指标计算；
                # QuikChange 模式另外计算协议经验 Tm 和估算自由能成本。
                forward_metrics = self.thermo.analyze(forward_sequence)
                reverse_metrics = self.thermo.analyze(reverse_sequence)
                quikchange_metrics = None
                if self.uses_quikchange:
                    wildtype_sequence = template[start:end]
                    quikchange_metrics = self.quikchange.analyze(
                        forward_sequence,
                        wildtype_sequence,
                        left_length,
                    )

                # 先判定硬性约束，再计算用于候选间排序的启发式分数。
                violations = self._violations(
                    forward_metrics,
                    reverse_metrics,
                    left_flank=left_length,
                    right_flank=right_length,
                    quikchange_metrics=quikchange_metrics,
                )
                advisories = self._advisories(
                    forward_sequence,
                    reverse_sequence,
                    forward_metrics,
                    reverse_metrics,
                )
                score = self._score(
                    forward_metrics,
                    reverse_metrics,
                    left_length,
                    right_length,
                    violations,
                    quikchange_metrics=quikchange_metrics,
                    advisories=advisories,
                )
                candidates.append(
                    PrimerPair(
                        mutation=mutation,
                        wildtype_codon=wildtype_codon,
                        replacement_codon=replacement_codon,
                        forward=Primer(mutation.name + "-F", "F", forward_sequence, forward_metrics),
                        reverse=Primer(mutation.name + "-R", "R", reverse_sequence, reverse_metrics),
                        score=score,
                        valid=not violations,
                        violations=tuple(violations),
                        left_flank_length=left_length,
                        right_flank_length=right_length,
                        quikchange_metrics=quikchange_metrics,
                        advisories=tuple(advisories),
                    )
                )
        return candidates

    def _violations(
        self,
        forward: PrimerMetrics,
        reverse: PrimerMetrics,
        *,
        left_flank: int | None = None,
        right_flank: int | None = None,
        quikchange_metrics: QuikChangeMetrics | None = None,
    ) -> list[str]:
        """返回一对引物违反的硬性约束说明；空列表代表候选合格。"""

        violations: list[str] = []
        metrics = (forward, reverse)
        if self.uses_quikchange:
            if any(m.length < self.config.min_length for m in metrics):
                violations.append(f"长度低于 {self.config.min_length} bp")
            if any(
                m.length > self.config.quikchange_absolute_max_length
                for m in metrics
            ):
                violations.append(
                    f"长度高于 QuikChange 绝对上限 "
                    f"{self.config.quikchange_absolute_max_length} bp"
                )
            if quikchange_metrics is None:
                violations.append("缺少 QuikChange Tm 指标")
            elif quikchange_metrics.tm_min < self.config.quikchange_min_tm:
                violations.append(
                    f"最低 QuikChange Tm 低于 "
                    f"{self.config.quikchange_min_tm:.2f}℃"
                )
            if (
                quikchange_metrics is not None
                and self.config.quikchange_max_tm is not None
                and quikchange_metrics.tm_max > self.config.quikchange_max_tm
            ):
                violations.append(
                    f"最高 QuikChange Tm 高于 "
                    f"{self.config.quikchange_max_tm:.2f}℃"
                )
            if left_flank is not None and left_flank < self.config.quikchange_min_flank:
                violations.append(
                    f"左侧翼短于 {self.config.quikchange_min_flank} bp"
                )
            if right_flank is not None and right_flank < self.config.quikchange_min_flank:
                violations.append(
                    f"右侧翼短于 {self.config.quikchange_min_flank} bp"
                )
        else:
            if any(m.length < self.config.min_length for m in metrics):
                violations.append(f"长度低于 {self.config.min_length} bp")
            if any(m.length > self.config.max_length for m in metrics):
                violations.append(f"长度高于 {self.config.max_length} bp")
            if any(m.tm_min < self.config.min_tm for m in metrics):
                violations.append(f"最低 Primer3 Tm 低于 {self.config.min_tm:.2f}℃")
            if any(m.tm_max > self.config.max_tm for m in metrics):
                violations.append(f"最高 Primer3 Tm 高于 {self.config.max_tm:.2f}℃")
            if any(m.gc_min < self.config.min_gc for m in metrics):
                violations.append(f"最低 GC 低于 {self.config.min_gc:.2f}%")
            if any(m.gc_max > self.config.max_gc for m in metrics):
                violations.append(f"最高 GC 高于 {self.config.max_gc:.2f}%")
        # 结构阈值默认为 None，此时只报告结构指标，不作为淘汰条件。
        if self.config.max_hairpin_tm is not None and any(
            m.hairpin_tm_max > self.config.max_hairpin_tm for m in metrics
        ):
            violations.append(f"发卡 Tm 高于 {self.config.max_hairpin_tm:.2f}℃")
        if self.config.max_homodimer_tm is not None and any(
            m.homodimer_tm_max > self.config.max_homodimer_tm for m in metrics
        ):
            violations.append(f"同源二聚体 Tm 高于 {self.config.max_homodimer_tm:.2f}℃")
        return violations

    def _advisories(
        self,
        forward_sequence: str,
        reverse_sequence: str,
        forward: PrimerMetrics,
        reverse: PrimerMetrics,
    ) -> list[str]:
        """返回不淘汰候选、但会参与 QuikChange 排序的优选项提示。"""

        if not self.uses_quikchange:
            return []
        advisories: list[str] = []
        metrics = (forward, reverse)
        if any(
            item.length > self.config.quikchange_preferred_max_length
            for item in metrics
        ):
            advisories.append(
                f"长度高于 QuikChange 优选上限 "
                f"{self.config.quikchange_preferred_max_length} bp"
            )
        if any(
            item.gc_min < self.config.quikchange_preferred_min_gc
            for item in metrics
        ):
            advisories.append(
                f"最低 GC 低于 QuikChange 优选值 "
                f"{self.config.quikchange_preferred_min_gc:.2f}%"
            )
        missing_clamps = []
        if forward_sequence[-1] not in "GC":
            missing_clamps.append("F")
        if reverse_sequence[-1] not in "GC":
            missing_clamps.append("R")
        if missing_clamps:
            advisories.append(
                f"{','.join(missing_clamps)} 引物 3' 端不是 G/C"
            )
        return advisories

    def _score(
        self,
        forward: PrimerMetrics,
        reverse: PrimerMetrics,
        left_flank: int,
        right_flank: int,
        violations: list[str],
        *,
        quikchange_metrics: QuikChangeMetrics | None = None,
        advisories: list[str] | tuple[str, ...] = (),
    ) -> float:
        """计算自定义候选惩罚分，分数越低排名越靠前。

        本分数不是 Primer3 或 QuikChange 官方分数。硬性约束每违反一类先增加
        1000 分；其余项目衡量 Tm、长度和突变居中程度与目标值的距离。
        """

        metrics = (forward, reverse)

        # QuikChange 模式使用协议经验 Tm、最短侧翼、软长度/GC/3' GC 和
        # Primer3 估算的自由能成本排名；用户设置的扩展目标只影响排序，
        # Primer3 Tm 仍仅作为输出指标。
        if self.uses_quikchange:
            penalty = 1000.0 * len(violations)
            if quikchange_metrics is not None:
                penalty += max(
                    0.0,
                    self.config.quikchange_min_tm - quikchange_metrics.tm_min,
                ) * 50
                penalty += quikchange_metrics.estimated_energy_cost_max
                if self.config.quikchange_opt_tm is None:
                    # 未启用扩展目标时保留原有行为：在满足最低 Tm 后，仅轻微
                    # 偏好更接近协议最低门槛的候选。
                    penalty += max(
                        0.0,
                        quikchange_metrics.tm_midpoint
                        - self.config.quikchange_min_tm,
                    ) * 0.05
                else:
                    penalty += abs(
                        quikchange_metrics.tm_midpoint
                        - self.config.quikchange_opt_tm
                    )
            penalty += max(0, self.config.min_length - forward.length) * 20
            if self.config.quikchange_opt_length is not None:
                penalty += abs(
                    forward.length - self.config.quikchange_opt_length
                ) * 0.08
            penalty += max(
                0,
                forward.length - self.config.quikchange_preferred_max_length,
            ) * 1.5
            penalty += max(
                0.0,
                self.config.quikchange_preferred_min_gc - forward.gc_min,
            ) * 2
            penalty += max(0, self.config.quikchange_min_flank - left_flank) * 20
            penalty += max(0, self.config.quikchange_min_flank - right_flank) * 20
            penalty += len(advisories) * 0.5
            penalty += abs(left_flank - right_flank) * 0.1
            for item in metrics:
                if self.config.max_hairpin_tm is not None:
                    penalty += max(
                        0.0, item.hairpin_tm_max - self.config.max_hairpin_tm
                    ) * 2
                if self.config.max_homodimer_tm is not None:
                    penalty += max(
                        0.0, item.homodimer_tm_max - self.config.max_homodimer_tm
                    ) * 2
            return penalty

        # 通用模式沿用原来的 Primer3 硬区间和目标值排名。
        penalty = 1000.0 * len(violations)
        for item in metrics:
            # 对超出允许范围的 Tm 和 GC 追加与超限幅度成比例的惩罚。
            penalty += max(0.0, self.config.min_tm - item.tm_min) * 25
            penalty += max(0.0, item.tm_max - self.config.max_tm) * 25
            penalty += max(0.0, self.config.min_gc - item.gc_min) * 5
            penalty += max(0.0, item.gc_max - self.config.max_gc) * 5
            # 即使候选合格，也会根据它与最优 Tm/长度的距离继续排序。
            penalty += abs(item.tm_midpoint - self.config.opt_tm)
            penalty += abs(item.length - self.config.opt_length) * 0.08
            if self.config.max_hairpin_tm is not None:
                penalty += max(0.0, item.hairpin_tm_max - self.config.max_hairpin_tm) * 2
            if self.config.max_homodimer_tm is not None:
                penalty += max(0.0, item.homodimer_tm_max - self.config.max_homodimer_tm) * 2
        # 左右侧翼越接近，说明突变越居中，惩罚越小。
        penalty += abs(left_flank - right_flank) * 0.1
        return penalty

    @staticmethod
    def _assign_rank(
        pair: PrimerPair, rank: int, *, near_miss: bool = False
    ) -> PrimerPair:
        """写入候选排名，并生成不会互相重复的引物名称。"""

        if near_miss:
            prefix = f"{pair.mutation.name}-NM{rank}"
        else:
            prefix = pair.mutation.name if rank == 1 else f"{pair.mutation.name}-C{rank}"
        return replace(
            pair,
            candidate_rank=rank,
            forward=replace(pair.forward, name=prefix + "-F"),
            reverse=replace(pair.reverse, name=prefix + "-R"),
        )


def design_primers(
    sequence: str,
    mutations: Iterable[str | MutationSpec],
    config: PrimerDesignConfig | None = None,
) -> BatchDesignResult:
    """无需手动实例化 ``PrimerDesigner`` 的便捷函数。"""

    return PrimerDesigner(config).design(sequence, mutations)
