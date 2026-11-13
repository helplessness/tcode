"""QuikChange II 协议专用指标计算。

本模块与通用 Primer3 指标分开，避免把两种 Tm 定义混在一起。
"""

from __future__ import annotations

import primer3

from .models import PrimerDesignConfig, QuikChangeMetrics
from .thermodynamics import expand_iupac, reverse_complement


class QuikChangeAnalyzer:
    """计算 QuikChange 经验 Tm 和错配双链的估算自由能成本。"""

    def __init__(self, config: PrimerDesignConfig):
        self.config = config
        self._cache: dict[tuple[str, str, int], QuikChangeMetrics] = {}
        self._perfect_duplex_dg_cache: dict[str, float] = {}

    def analyze(
        self,
        primer_sequence: str,
        wildtype_sequence: str,
        mutation_offset: int,
    ) -> QuikChangeMetrics:
        """分析一条引物相对同长度母本片段的 QuikChange 指标。

        ``mutation_offset`` 是突变密码子在正向引物中的 0-based 起点。
        简并密码子会展开所有具体变体并报告完整范围。
        """

        primer_sequence = primer_sequence.upper()
        wildtype_sequence = wildtype_sequence.upper()
        key = (primer_sequence, wildtype_sequence, mutation_offset)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if len(primer_sequence) != len(wildtype_sequence):
            raise ValueError("QuikChange 指标要求引物与母本片段长度一致")

        wildtype_codon = wildtype_sequence[mutation_offset:mutation_offset + 3]
        template_complement = reverse_complement(wildtype_sequence)
        perfect_dg = self._perfect_duplex_dg(wildtype_sequence)
        tms: list[float] = []
        mismatches: list[float] = []
        energy_costs: list[float] = []

        for concrete in expand_iupac(
            primer_sequence,
            max_variants=self.config.max_degenerate_variants,
        ):
            gc_percent = 100 * (
                concrete.count("G") + concrete.count("C")
            ) / len(concrete)
            concrete_codon = concrete[mutation_offset:mutation_offset + 3]
            mismatch_count = sum(
                actual != expected
                for actual, expected in zip(concrete_codon, wildtype_codon)
            )
            mismatch_percent = 100 * mismatch_count / len(concrete)
            tms.append(
                81.5
                + 0.41 * gc_percent
                - 675 / len(concrete)
                - mismatch_percent
            )
            mismatches.append(mismatch_percent)

            mismatched_dg = primer3.bindings.calc_heterodimer(
                concrete,
                template_complement,
                mv_conc=self.config.mv_conc,
                dv_conc=self.config.dv_conc,
                dntp_conc=self.config.dntp_conc,
                dna_conc=self.config.dna_conc,
                temp_c=self.config.quikchange_energy_temp_c,
            ).dg
            if perfect_dg:
                energy_costs.append((perfect_dg - mismatched_dg) / perfect_dg * 100)
            else:
                energy_costs.append(0.0)

        metrics = QuikChangeMetrics(
            tm_min=min(tms),
            tm_max=max(tms),
            mismatch_percent_min=min(mismatches),
            mismatch_percent_max=max(mismatches),
            estimated_energy_cost_min=min(energy_costs),
            estimated_energy_cost_max=max(energy_costs),
        )
        self._cache[key] = metrics
        return metrics

    def _perfect_duplex_dg(self, wildtype_sequence: str) -> float:
        """缓存同长度无错配母本双链的自由能。"""

        cached = self._perfect_duplex_dg_cache.get(wildtype_sequence)
        if cached is not None:
            return cached
        value = primer3.bindings.calc_heterodimer(
            wildtype_sequence,
            reverse_complement(wildtype_sequence),
            mv_conc=self.config.mv_conc,
            dv_conc=self.config.dv_conc,
            dntp_conc=self.config.dntp_conc,
            dna_conc=self.config.dna_conc,
            temp_c=self.config.quikchange_energy_temp_c,
        ).dg
        self._perfect_duplex_dg_cache[wildtype_sequence] = value
        return value
