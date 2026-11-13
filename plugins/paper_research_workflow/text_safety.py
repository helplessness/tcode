"""文献抽取文本的语义安全辅助函数。"""

from __future__ import annotations

import re


# PDF 字体映射失败时常残留 /H9252D、/H110021 一类内部字形编号。
PDF_CONTROL_CODE_RE = re.compile(r"/H[0-9A-F]{4,8}[A-Za-z]?", re.IGNORECASE)

_ONE_LETTER_AMINO_ACIDS = "ACDEFGHIKLMNPQRSTVWY"
_THREE_LETTER_AMINO_ACIDS = (
    "Ala|Arg|Asn|Asp|Cys|Gln|Glu|Gly|His|Ile|Leu|Lys|Met|Phe|Pro|Ser|Thr|Trp|Tyr|Val"
)
MUTATION_RE = re.compile(
    rf"(?<![/A-Za-z0-9])(?:[{_ONE_LETTER_AMINO_ACIDS}]|(?:{_THREE_LETTER_AMINO_ACIDS}))"
    rf"\d{{1,4}}(?:[{_ONE_LETTER_AMINO_ACIDS}]|(?:{_THREE_LETTER_AMINO_ACIDS}))"
    rf"(?![A-Za-z0-9])"
)


def strip_pdf_control_codes(value: object) -> str:
    """移除 PDF 内部字形控制码，避免其进入实体、摘要与 Wiki。"""

    return PDF_CONTROL_CODE_RE.sub("", str(value or ""))


def contains_pdf_control_code(value: object) -> bool:
    """判断文本是否仍含未清理的 PDF 内部字形控制码。"""

    return PDF_CONTROL_CODE_RE.search(str(value or "")) is not None


def find_valid_mutations(value: object) -> list[str]:
    """返回顺序稳定且去重的标准氨基酸替换实体。"""

    sanitized = strip_pdf_control_codes(value)
    return list(dict.fromkeys(match.group(0) for match in MUTATION_RE.finditer(sanitized)))


def is_valid_mutation(value: object) -> bool:
    """校验单个值是否恰好是标准氨基酸替换表达式。"""

    text = str(value or "").strip()
    return not contains_pdf_control_code(text) and MUTATION_RE.fullmatch(text) is not None
