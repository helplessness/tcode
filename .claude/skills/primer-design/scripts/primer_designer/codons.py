"""标准遗传密码表、DNA 翻译和目标密码子选择工具。"""

from __future__ import annotations

from collections import defaultdict


# 标准遗传密码表：键为 DNA 密码子，值为单字母氨基酸代码。
GENETIC_CODE: dict[str, str] = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*",
    "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L",
    "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M",
    "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K",
    "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V",
    "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}

# 反向建立“氨基酸 -> 所有同义密码子”的索引，供突变密码子选择使用。
_codons_by_aa: defaultdict[str, list[str]] = defaultdict(list)
for _codon, _aa in GENETIC_CODE.items():
    _codons_by_aa[_aa].append(_codon)
CODONS_BY_AA: dict[str, tuple[str, ...]] = {
    aa: tuple(codons) for aa, codons in _codons_by_aa.items()
}


def translate_codon(codon: str) -> str:
    """把一个三碱基 DNA 密码子翻译成单字母氨基酸。"""

    return GENETIC_CODE[codon.upper()]


def translate_dna(sequence: str) -> str:
    """按照标准遗传密码表翻译完整 CDS。"""

    sequence = sequence.upper()
    if len(sequence) % 3:
        raise ValueError("DNA 序列长度不是 3 的倍数")
    return "".join(translate_codon(sequence[i:i + 3]) for i in range(0, len(sequence), 3))


def hamming_distance(left: str, right: str) -> int:
    """计算两个等长字符串之间不同字符的数量。"""

    if len(left) != len(right):
        raise ValueError("Hamming distance 要求两个字符串等长")
    return sum(a != b for a, b in zip(left, right))


def minimal_change_codons(target_aa: str, source_codon: str) -> tuple[str, ...]:
    """返回编码目标氨基酸且相对母本密码子改动最少的密码子。

    如果存在多个并列密码子，本函数会全部返回，由后续引物评分决定最终排名。
    """

    codons = CODONS_BY_AA.get(target_aa.upper())
    if not codons:
        raise ValueError(f"不支持目标氨基酸：{target_aa}")
    distances = {codon: hamming_distance(codon, source_codon) for codon in codons}
    minimum = min(distances.values())
    return tuple(codon for codon in codons if distances[codon] == minimum)
