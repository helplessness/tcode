"""为普通包加载和独立 Wiki 包加载统一提供文本安全函数。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


try:
    from ..text_safety import (
        contains_pdf_control_code,
        find_valid_mutations,
        is_valid_mutation,
        strip_pdf_control_codes,
    )
except ImportError:
    # PluginManager 可把 wiki 目录临时加载为顶层包，此时父级相对导入不可用。
    _TEXT_SAFETY_PATH = Path(__file__).resolve().parents[1] / "text_safety.py"
    _TEXT_SAFETY_SPEC = importlib.util.spec_from_file_location(
        "paper_research_workflow_wiki_text_safety",
        _TEXT_SAFETY_PATH,
    )
    if _TEXT_SAFETY_SPEC is None or _TEXT_SAFETY_SPEC.loader is None:
        raise ImportError(f"cannot load paper text safety module: {_TEXT_SAFETY_PATH}")
    _TEXT_SAFETY_MODULE = importlib.util.module_from_spec(_TEXT_SAFETY_SPEC)
    _TEXT_SAFETY_SPEC.loader.exec_module(_TEXT_SAFETY_MODULE)
    contains_pdf_control_code = _TEXT_SAFETY_MODULE.contains_pdf_control_code
    find_valid_mutations = _TEXT_SAFETY_MODULE.find_valid_mutations
    is_valid_mutation = _TEXT_SAFETY_MODULE.is_valid_mutation
    strip_pdf_control_codes = _TEXT_SAFETY_MODULE.strip_pdf_control_codes


__all__ = [
    "contains_pdf_control_code",
    "find_valid_mutations",
    "is_valid_mutation",
    "strip_pdf_control_codes",
]
