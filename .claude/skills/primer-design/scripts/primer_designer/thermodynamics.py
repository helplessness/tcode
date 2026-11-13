"""IUPAC 简并序列处理和 Primer3 热力学指标计算。"""

from __future__ import annotations

import math
from itertools import product

import primer3

from .exceptions import DegenerateExpansionError
from .models import PrimerDesignConfig, PrimerMetrics


# 每个 IUPAC 碱基代码所代表的具体 DNA 碱基集合。
IUPAC: dict[str, str] = {
    "A": "A", "C": "C", "G": "G", "T": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT",
    "K": "GT", "M": "AC", "B": "CGT", "D": "AGT",
    "H": "ACT", "V": "ACG", "N": "ACGT",
}

# IUPAC 反向互补映射，例如 K(G/T) 的互补代码为 M(A/C)。
IUPAC_COMPLEMENT = str.maketrans(
    "ACGTRYSWKMBDHVN",
    "TGCAYRSWMKVHDBN",
)


def reverse_complement(sequence: str) -> str:
    """计算支持 IUPAC 简并碱基的反向互补序列。"""

    sequence = sequence.upper()
    invalid = sorted(set(sequence) - set(IUPAC))
    if invalid:
        raise ValueError(f"序列包含不支持的 IUPAC 碱基：{''.join(invalid)}")
    return sequence.translate(IUPAC_COMPLEMENT)[::-1]


def variant_count(sequence: str) -> int:
    """计算一个简并序列能够展开成多少条具体 A/C/G/T 序列。"""

    return math.prod(len(IUPAC[base]) for base in sequence.upper())


def expand_iupac(sequence: str, *, max_variants: int = 4096):
    """逐条生成简并序列的具体变体，并限制最大展开数量。"""

    sequence = sequence.upper()
    invalid = sorted(set(sequence) - set(IUPAC))
    if invalid:
        raise ValueError(f"序列包含不支持的 IUPAC 碱基：{''.join(invalid)}")
    count = variant_count(sequence)
    if count > max_variants:
        raise DegenerateExpansionError(
            f"简并序列将展开为 {count} 个变体，超过上限 {max_variants}"
        )
    pools = [IUPAC[base] for base in sequence]
    for bases in product(*pools):
        yield "".join(bases)


class ThermodynamicsAnalyzer:
    """使用 Primer3 计算引物的 Tm、GC、发卡和同源二聚体指标。"""

    def __init__(self, config: PrimerDesignConfig):
        self.config = config
        # 同一候选可能被重复访问，缓存可避免重复执行 Primer3 计算。
        self._cache: dict[str, PrimerMetrics] = {}

    def analyze(self, sequence: str) -> PrimerMetrics:
        """分析一条普通或简并引物，并返回单值或全变体范围。"""

        sequence = sequence.upper()
        cached = self._cache.get(sequence)
        if cached is not None:
            return cached

        tms: list[float] = []
        gcs: list[float] = []
        hairpins: list[float] = []
        homodimers: list[float] = []
        count = 0
        # 普通引物只循环一次；NNK 会循环 4×4×2=32 次。
        for concrete in expand_iupac(
            sequence, max_variants=self.config.max_degenerate_variants
        ):
            count += 1
            # Tm 使用配置中的盐、Mg²⁺、dNTP、引物浓度和 DMSO 条件。
            tms.append(
                primer3.bindings.calc_tm(
                    concrete,
                    mv_conc=self.config.mv_conc,
                    dv_conc=self.config.dv_conc,
                    dntp_conc=self.config.dntp_conc,
                    dna_conc=self.config.dna_conc,
                    dmso_conc=self.config.dmso_conc,
                )
            )
            gcs.append(100 * (concrete.count("G") + concrete.count("C")) / len(concrete))

            # 结构指标按每个具体序列分别计算，最终保留最不利的最大值。
            hairpins.append(
                primer3.bindings.calc_hairpin(
                    concrete,
                    mv_conc=self.config.mv_conc,
                    dv_conc=self.config.dv_conc,
                    dntp_conc=self.config.dntp_conc,
                    dna_conc=self.config.dna_conc,
                ).tm
            )
            homodimers.append(
                primer3.bindings.calc_homodimer(
                    concrete,
                    mv_conc=self.config.mv_conc,
                    dv_conc=self.config.dv_conc,
                    dntp_conc=self.config.dntp_conc,
                    dna_conc=self.config.dna_conc,
                ).tm
            )

        metrics = PrimerMetrics(
            length=len(sequence),
            variant_count=count,
            tm_min=min(tms),
            tm_max=max(tms),
            gc_min=min(gcs),
            gc_max=max(gcs),
            hairpin_tm_max=max(hairpins),
            homodimer_tm_max=max(homodimers),
        )
        self._cache[sequence] = metrics
        return metrics
