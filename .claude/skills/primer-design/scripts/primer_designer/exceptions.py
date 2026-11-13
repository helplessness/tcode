"""包内可预期错误的统一异常层次。"""


class PrimerDesignError(Exception):
    """引物设计包的基础异常。"""


class SequenceValidationError(PrimerDesignError):
    """输入 DNA/CDS 序列无效。"""


class MutationValidationError(PrimerDesignError):
    """突变描述、坐标或母本氨基酸无效。"""


class ConfigurationError(PrimerDesignError):
    """设计参数无效。"""


class DegenerateExpansionError(PrimerDesignError):
    """简并序列展开数量超出限制。"""


class ExternalPrimerValidationError(PrimerDesignError):
    """外部引物文件、序列或与突变位点的对应关系无效。"""
