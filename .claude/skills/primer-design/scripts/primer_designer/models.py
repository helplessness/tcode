"""引物设计过程中使用的数据模型和参数对象。

本模块只定义“数据长什么样”，不负责生成引物。调用方通常只需要构造
``PrimerDesignConfig`` 和 ``MutationSpec``，其余对象由设计器返回。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .exceptions import ConfigurationError, MutationValidationError


class MutationType(str, Enum):
    """支持的突变描述类型。"""

    FIXED = "fixed"
    DEGENERATE = "degenerate"


class DesignPreset(str, Enum):
    """候选判定和排名所采用的设计规则。"""

    QUIKCHANGE_II = "quikchange_ii"
    PRIMER3_GENERIC = "primer3_generic"


@dataclass(frozen=True)
class MutationSpec:
    """描述一个氨基酸位点上的突变任务。

    ``position`` 使用从 1 开始的氨基酸坐标；定点突变使用 ``target_aa``，
    NNK/NNS 等饱和突变使用 ``degenerate_codon``。
    """

    position: int
    mutation_type: MutationType
    reference_aa: str | None = None
    target_aa: str | None = None
    replacement_codon: str | None = None
    degenerate_codon: str | None = None
    label: str | None = None

    @classmethod
    def parse(cls, value: str) -> "MutationSpec":
        """把 ``C231A``、``C231NNK`` 等字符串解析为结构化突变对象。"""

        text = value.strip().upper()

        # 定点突变格式：母本氨基酸 + 位置 + 目标氨基酸，例如 C231A。
        fixed = re.fullmatch(r"([A-Z*])(\d+)([A-Z*])(?::([ACGT]{3}))?", text)
        if fixed:
            ref, position, target, codon = fixed.groups()
            # 指定密码子时仍使用 C231A 作为输出名称，避免冒号进入引物名称。
            label = f"{ref}{position}{target}"
            return cls.fixed(
                int(position), target, reference_aa=ref, codon=codon, label=label
            )

        # 简并突变格式允许省略母本氨基酸，例如 C231NNK 或 231NNK。
        degenerate = re.fullmatch(r"([A-Z*])?(\d+)(NNK|NNS)", text)
        if degenerate:
            ref, position, pattern = degenerate.groups()
            return cls.degenerate(
                int(position), pattern=pattern, reference_aa=ref, label=text
            )
        raise MutationValidationError(
            f"无法解析突变“{value}”；支持 C231A、C231A:GCC、C231NNK、231NNK 等格式"
        )


    @classmethod
    def fixed(
        cls,
        position: int,
        target_aa: str,
        *,
        reference_aa: str | None = None,
        codon: str | None = None,
        label: str | None = None,
    ) -> "MutationSpec":
        """显式创建定点突变，可选指定最终使用的目标密码子。"""

        return cls(
            position=position,
            mutation_type=MutationType.FIXED,
            reference_aa=reference_aa.upper() if reference_aa else None,
            target_aa=target_aa.upper(),
            replacement_codon=codon.upper() if codon else None,
            label=label,
        )

    @classmethod
    def degenerate(
        cls,
        position: int,
        *,
        pattern: str = "NNK",
        reference_aa: str | None = None,
        label: str | None = None,
    ) -> "MutationSpec":
        """显式创建 NNK、NNS 或其他三碱基 IUPAC 简并突变。"""

        return cls(
            position=position,
            mutation_type=MutationType.DEGENERATE,
            reference_aa=reference_aa.upper() if reference_aa else None,
            degenerate_codon=pattern.upper(),
            label=label,
        )

    @property
    def name(self) -> str:
        """返回用于输出和引物命名的突变名称。"""

        if self.label:
            return self.label
        prefix = self.reference_aa or ""
        suffix = self.target_aa if self.mutation_type is MutationType.FIXED else self.degenerate_codon
        return f"{prefix}{self.position}{suffix}"


@dataclass(frozen=True)
class PrimerDesignConfig:
    """控制候选生成、筛选和热力学计算的全部参数。

    ``opt_length``、``opt_tm`` 和通用 Tm/GC 区间仅供 ``primer3_generic``
    使用。QuikChange 的扩展目标使用独立的 ``quikchange_opt_*`` 字段，避免
    两套预设的同名参数产生歧义。``strict=True`` 时，主要结果中只保留完全
    合格的候选。
    """

    # 设计规则预设。默认使用当前需求对应的 QuikChange II 规则；需要沿用
    # 原来的长度/Tm/GC 硬区间时，显式选择 primer3_generic。
    preset: str = DesignPreset.QUIKCHANGE_II.value

    # 当前实现的实验策略名称；其他策略需要实现独立的候选生成规则。
    strategy: str = "overlap_complementary"

    # 引物长度约束和排序目标，单位为 bp。
    min_length: int = 25
    opt_length: int = 35
    max_length: int = 45

    # 引物熔解温度约束和排序目标，单位为摄氏度。
    min_tm: float = 65.0
    opt_tm: float = 70.0
    max_tm: float = 75.0

    # GC 百分比允许范围。
    min_gc: float = 40.0
    max_gc: float = 65.0

    # QuikChange II 专用参数。45 bp、40% GC 和 3' GC 是协议优选项，不作为
    # 硬淘汰条件；最低 Tm、绝对长度上限和最短侧翼是硬约束。
    quikchange_min_tm: float = 78.0
    quikchange_preferred_max_length: int = 45
    quikchange_absolute_max_length: int = 60
    # 对三碱基密码子，外侧 9 bp 等价于中央突变碱基距末端至少 10 个位置。
    quikchange_min_flank: int = 9
    quikchange_preferred_min_gc: float = 40.0
    quikchange_energy_temp_c: float = 68.0

    # 可选的 QuikChange 扩展规则。默认关闭，以保留官方公开规则和旧版排名；
    # opt 值只影响合格候选之间的排序，max_tm 是用户自定义的硬性上限。
    quikchange_opt_length: int | None = None
    quikchange_opt_tm: float | None = None
    quikchange_max_tm: float | None = None

    # Primer3 热力学计算所需的反应体系参数。
    mv_conc: float = 50.0
    dv_conc: float = 1.5
    dntp_conc: float = 0.6
    dna_conc: float = 50.0
    dmso_conc: float = 0.0

    # 允许突变密码子偏离几何中心的最大碱基数。
    center_slack: int = 2

    # 每个位点返回的合格候选数和近似候选数。
    num_return: int = 3
    near_miss_count: int = 3

    # 严格模式只返回合格候选；宽松模式允许超限候选进入主要结果。
    strict: bool = True

    # 搜索近似候选时，可在长度上下限之外额外扩展的碱基数。
    relax_length_by: int = 3

    # CDS 输入校验开关。
    validate_cds: bool = True
    require_start_codon: bool = True
    require_terminal_stop: bool = True
    reject_internal_stops: bool = True

    # 设置为数值后，发卡和同源二聚体才会参与硬性筛选。
    max_hairpin_tm: float | None = None
    max_homodimer_tm: float | None = None

    # 防止包含多个简并位点时出现指数级序列展开。
    max_degenerate_variants: int = 4096

    def __post_init__(self) -> None:
        """在配置创建时尽早检查参数组合是否合法。"""

        try:
            normalized_preset = DesignPreset(self.preset).value
        except ValueError as exc:
            choices = "、".join(item.value for item in DesignPreset)
            raise ConfigurationError(f"preset 必须为 {choices} 之一") from exc
        object.__setattr__(self, "preset", normalized_preset)
        if self.strategy != "overlap_complementary":
            raise ConfigurationError(
                "当前版本仅支持 overlap_complementary 策略"
            )
        if not 1 <= self.min_length <= 60:
            raise ConfigurationError("最短引物长度必须位于 1～60 bp")

        # 只校验当前预设真正会使用的参数。这样在 QuikChange 模式下修改
        # min_length 时，不会被无关的通用 opt_length 默认值意外阻止。
        if normalized_preset == DesignPreset.PRIMER3_GENERIC.value:
            if not self.min_length <= self.opt_length <= self.max_length <= 60:
                raise ConfigurationError(
                    "通用引物长度必须满足 min_length <= opt_length <= max_length <= 60"
                )
            if not self.min_tm <= self.opt_tm <= self.max_tm:
                raise ConfigurationError("通用 Tm 必须满足 min_tm <= opt_tm <= max_tm")
            if not 0 <= self.min_gc <= self.max_gc <= 100:
                raise ConfigurationError("通用 GC 必须位于 0～100 且 min_gc <= max_gc")
        else:
            if self.quikchange_min_tm <= 0:
                raise ConfigurationError("QuikChange 最低 Tm 必须大于 0")
            if not (
                self.min_length
                <= self.quikchange_preferred_max_length
                <= self.quikchange_absolute_max_length
                <= 60
            ):
                raise ConfigurationError(
                    "QuikChange 长度必须满足 min_length <= preferred_max <= "
                    "absolute_max <= 60"
                )
            if self.quikchange_opt_length is not None and not (
                self.min_length
                <= self.quikchange_opt_length
                <= self.quikchange_preferred_max_length
            ):
                raise ConfigurationError(
                    "QuikChange 目标长度必须满足 min_length <= qc_opt_length <= "
                    "preferred_max"
                )
            if self.quikchange_max_tm is not None:
                if self.quikchange_max_tm < self.quikchange_min_tm:
                    raise ConfigurationError(
                        "QuikChange Tm 必须满足 qc_min_tm <= qc_max_tm"
                    )
            if self.quikchange_opt_tm is not None:
                upper_tm = (
                    self.quikchange_max_tm
                    if self.quikchange_max_tm is not None
                    else float("inf")
                )
                if not self.quikchange_min_tm <= self.quikchange_opt_tm <= upper_tm:
                    raise ConfigurationError(
                        "QuikChange 目标 Tm 必须满足 qc_min_tm <= qc_opt_tm"
                        " <= qc_max_tm（若设置）"
                    )
            if self.quikchange_min_flank < 1:
                raise ConfigurationError("QuikChange 最短侧翼必须至少为 1 bp")
            if not 0 <= self.quikchange_preferred_min_gc <= 100:
                raise ConfigurationError("QuikChange 优选 GC 下限必须位于 0～100")
            if self.quikchange_energy_temp_c <= 0:
                raise ConfigurationError("QuikChange 自由能计算温度必须大于 0")
        if self.center_slack < 0 or self.relax_length_by < 0:
            raise ConfigurationError("center_slack 和 relax_length_by 不能为负数")
        if self.num_return < 1 or self.near_miss_count < 0:
            raise ConfigurationError("num_return 至少为 1，near_miss_count 不能为负数")
        if self.max_degenerate_variants < 1:
            raise ConfigurationError("max_degenerate_variants 至少为 1")


@dataclass(frozen=True)
class PrimerMetrics:
    """保存一条引物的长度和热力学指标。

    对普通引物，最小值和最大值相同；对 NNK 等简并引物，它们表示所有
    具体序列变体中的指标范围。
    """

    length: int
    variant_count: int
    tm_min: float
    tm_max: float
    gc_min: float
    gc_max: float
    hairpin_tm_max: float
    homodimer_tm_max: float

    @property
    def tm_midpoint(self) -> float:
        """返回 Tm 范围中点，用于候选排序。"""

        return (self.tm_min + self.tm_max) / 2


@dataclass(frozen=True)
class QuikChangeMetrics:
    """QuikChange II 经验公式和错配双链的协议相关指标。

    网站使用的旧版 mfold 数据无法在本地逐项复刻，因此自由能成本使用
    Primer3 最近邻模型估算，并在字段名中明确标为 estimated。
    """

    tm_min: float
    tm_max: float
    mismatch_percent_min: float
    mismatch_percent_max: float
    estimated_energy_cost_min: float
    estimated_energy_cost_max: float

    @property
    def tm_midpoint(self) -> float:
        """返回 QuikChange Tm 范围中点。"""

        return (self.tm_min + self.tm_max) / 2

    @property
    def estimated_energy_cost_midpoint(self) -> float:
        """返回估算自由能成本范围中点。"""

        return (
            self.estimated_energy_cost_min + self.estimated_energy_cost_max
        ) / 2


@dataclass(frozen=True)
class Primer:
    """一条可订购的正向或反向引物及其计算指标。"""

    name: str
    direction: str
    sequence: str
    metrics: PrimerMetrics


@dataclass(frozen=True)
class PrimerPair:
    """同一突变对应的一对正反向引物和候选评分。"""

    mutation: MutationSpec
    wildtype_codon: str
    replacement_codon: str
    forward: Primer
    reverse: Primer
    score: float
    valid: bool
    violations: tuple[str, ...] = ()
    candidate_rank: int = 0
    left_flank_length: int = 0
    right_flank_length: int = 0
    quikchange_metrics: QuikChangeMetrics | None = None
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True)
class MutationDesignResult:
    """单个突变位点的合格候选、近似候选和警告信息。"""

    mutation: MutationSpec
    candidates: tuple[PrimerPair, ...] = ()
    near_misses: tuple[PrimerPair, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def best(self) -> PrimerPair | None:
        """返回排名第一的主要候选；没有合格候选时返回 ``None``。"""

        return self.candidates[0] if self.candidates else None

    @property
    def has_valid_candidate(self) -> bool:
        """判断主要候选中是否至少有一对完全满足约束。"""

        return any(pair.valid for pair in self.candidates)


@dataclass(frozen=True)
class BatchDesignResult:
    """一次批量设计任务的完整结果。"""

    sequence: str
    results: tuple[MutationDesignResult, ...]
    config: PrimerDesignConfig
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_succeeded(self) -> bool:
        """所有突变都有合格候选时返回 ``True``。"""

        return all(result.has_valid_candidate for result in self.results)

    def iter_pairs(self, *, include_near_misses: bool = False) -> Iterable[PrimerPair]:
        """依次遍历所有候选引物对，可选包含近似候选。"""

        for result in self.results:
            yield from result.candidates
            if include_near_misses:
                yield from result.near_misses
