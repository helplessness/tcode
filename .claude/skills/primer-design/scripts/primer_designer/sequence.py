"""DNA/FASTA 输入清洗和 CDS 合法性校验。"""

from __future__ import annotations

import re

from .codons import translate_dna
from .exceptions import SequenceValidationError
from .models import PrimerDesignConfig


def normalize_dna(raw: str) -> str:
    """把 FASTA 或纯文本 DNA 统一转换为大写、无空白的 A/C/G/T 序列。"""

    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        # FASTA 中以 > 开头的标题行不属于 DNA 序列。
        if not stripped or stripped.startswith(">"):
            continue
        lines.append(stripped)
    sequence = re.sub(r"\s+", "", "".join(lines)).upper()
    if not sequence:
        raise SequenceValidationError("DNA 序列为空")
    invalid = sorted(set(sequence) - set("ACGT"))
    if invalid:
        raise SequenceValidationError(f"母本序列只能包含 A/C/G/T，发现：{''.join(invalid)}")
    return sequence


def validate_cds(sequence: str, config: PrimerDesignConfig) -> str:
    """按照配置检查起始、终止和内部终止密码子，并返回翻译产物。"""

    if not config.validate_cds:
        return ""

    # 完整 CDS 必须能按三个碱基一组进行翻译。
    if len(sequence) % 3:
        raise SequenceValidationError("CDS 长度不是 3 的倍数")
    if config.require_start_codon and not sequence.startswith("ATG"):
        raise SequenceValidationError("CDS 不是以 ATG 起始")
    protein = translate_dna(sequence)

    # 星号代表终止密码子；默认只允许它出现在蛋白最后一位。
    if config.require_terminal_stop and protein[-1] != "*":
        raise SequenceValidationError("CDS 末尾不是终止密码子")
    if config.reject_internal_stops and "*" in protein[:-1]:
        positions = [str(i + 1) for i, aa in enumerate(protein[:-1]) if aa == "*"]
        raise SequenceValidationError(f"CDS 存在内部终止密码子，氨基酸位置：{', '.join(positions)}")
    return protein
