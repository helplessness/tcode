"""Agent 运行时门控模块。

基于已有的 ToolCapability 类型系统，在 Agent 准备 finalize 时进行轻量级检查，
不引入额外 LLM 调用，不依赖硬编码的枚举列表。
"""

from .final_answer_guard import (
    check_thin_trace,
    check_hallucinated_tool_refs,
    check_incomplete_paper_json,
    guard_enabled,
)
from .outcome_guard import (
    OutcomeValidationResult,
    apply_user_outcome_confirmation,
    normalize_outcome_candidate,
    outcome_guard_enabled,
    public_outcome_projection,
    validate_outcome,
    validate_outcome_deterministic,
)
from .deterministic_verifiers import (
    CriterionVerificationResult,
    deterministic_verifier_enabled,
    verifier_input_digests,
    verify_contract_criteria,
    verify_criterion_deterministic,
)
from .proof_levels import (
    PROOF_LEVELS,
    calculate_achieved_proof_level,
    normalize_proof_level,
    proof_level_satisfies,
)
from .verifier_registry import (
    VERIFIER_INPUT_SCHEMA,
    VERIFIER_OUTPUT_SCHEMA,
    VerifierDefinition,
    VerifierRegistry,
    VerifierRegistrationError,
    verifier_input_digest,
)
