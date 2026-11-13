"""引物设计包的公开 Python API。

调用方应优先从 ``primer_designer`` 导入对象，而不是依赖内部模块路径。
"""

# 核心设计入口。
from .designer import PrimerDesigner, design_primers

# 对外公开统一异常，便于 Skill 或应用层集中捕获。
from .exceptions import (
    ConfigurationError,
    DegenerateExpansionError,
    ExternalPrimerValidationError,
    MutationValidationError,
    PrimerDesignError,
    SequenceValidationError,
)
from .external import (
    ExternalPrimerEvaluation,
    ExternalPrimerEvaluator,
    ExternalPrimerPair,
    evaluate_external_primers,
    export_external_evaluations,
    read_external_primers,
)
# 结果序列化和文件导出入口。
from .exporters import export_csv, export_xlsx, result_rows

# 输入参数和结果数据结构。
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

__all__ = [
    "BatchDesignResult",
    "ConfigurationError",
    "DesignPreset",
    "DegenerateExpansionError",
    "ExternalPrimerEvaluation",
    "ExternalPrimerEvaluator",
    "ExternalPrimerPair",
    "ExternalPrimerValidationError",
    "MutationDesignResult",
    "MutationSpec",
    "MutationType",
    "MutationValidationError",
    "Primer",
    "PrimerDesignConfig",
    "PrimerDesignError",
    "PrimerDesigner",
    "PrimerMetrics",
    "PrimerPair",
    "QuikChangeMetrics",
    "SequenceValidationError",
    "design_primers",
    "evaluate_external_primers",
    "export_csv",
    "export_xlsx",
    "export_external_evaluations",
    "read_external_primers",
    "result_rows",
]
