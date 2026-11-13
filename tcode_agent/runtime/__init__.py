"""Runtime infrastructure helpers for task execution.

Use lazy exports here so lightweight modules (for example execution profiles)
do not pull in the whole runtime/provider stack at import time.
"""

from __future__ import annotations

__all__ = [
    "DuplicateServiceError",
    "EffectDisposalError",
    "EffectDisposalFailure",
    "EffectScope",
    "EffectScopeClosedError",
    "EffectScopeState",
    "EffectTransaction",
    "EffectTransactionError",
    "EffectTransactionState",
    "EffectTransactionStateError",
    "RegistrationDisposer",
    "RegistrationToken",
    "RuntimeContext",
    "RuntimeContextCloseError",
    "RuntimeContextCloseFailure",
    "RuntimeContextClosedError",
    "RuntimeContextError",
    "RuntimeContextState",
    "RuntimeIdentity",
    "ServiceCardinality",
    "ServiceDefinitionConflictError",
    "ServiceKey",
    "ServiceNotFoundError",
    "ServiceProvider",
    "ServiceProviderContractError",
    "ServiceProviderPlan",
    "ServiceProviderPlanError",
    "ServiceProviderTypeError",
    "ServiceRegistrationDisposer",
    "ServiceRegistrationToken",
    "apply_service_providers",
    "plan_service_providers",
    "EVENTS",
    "JOBS",
    "MEMORY",
    "RUNTIME_SERVICE_KEYS",
    "PROMPT",
    "SESSION_PROJECTION",
    "TOOLS",
    "RuntimeServiceProvider",
    "RuntimeServiceUnavailableError",
    "create_runtime_context",
    "RuntimeArtifactManager",
    "SessionArchiveManager",
    "RuntimeTaskManager",
    "TaskRuntimeStore",
    "task_runtime_id_for_tool_call",
    "task_runtime_status_for_tool_result",
    "run_preflight_checks",
    "run_postflight_checks",
    "max_execution_retries",
    "retry_decision_for_exception",
    "SharedTracker",
    "ExecutionProfile",
    "get_execution_profile_char_budget",
    "get_execution_profile_prompt_budget_tokens",
    "get_execution_profile",
    "is_known_execution_profile",
    "list_execution_profiles",
    "normalize_execution_profile",
    "profile_allows_tool",
]


def __getattr__(name: str):
    """按需导入运行时公开符号，避免轻量模块加载完整依赖图。"""

    if name in {"RegistrationDisposer", "RegistrationToken"}:
        from .registrations import RegistrationDisposer, RegistrationToken

        return {
            "RegistrationDisposer": RegistrationDisposer,
            "RegistrationToken": RegistrationToken,
        }[name]
    if name in {
        "DuplicateServiceError",
        "RuntimeContext",
        "RuntimeContextCloseError",
        "RuntimeContextCloseFailure",
        "RuntimeContextClosedError",
        "RuntimeContextError",
        "RuntimeContextState",
        "RuntimeIdentity",
        "ServiceCardinality",
        "ServiceDefinitionConflictError",
        "ServiceKey",
        "ServiceNotFoundError",
        "ServiceProvider",
        "ServiceProviderContractError",
        "ServiceProviderPlan",
        "ServiceProviderPlanError",
        "ServiceProviderTypeError",
        "ServiceRegistrationDisposer",
        "ServiceRegistrationToken",
        "apply_service_providers",
        "plan_service_providers",
    }:
        from .context import (
            DuplicateServiceError,
            RuntimeContext,
            RuntimeContextCloseError,
            RuntimeContextCloseFailure,
            RuntimeContextClosedError,
            RuntimeContextError,
            RuntimeContextState,
            RuntimeIdentity,
            ServiceCardinality,
            ServiceDefinitionConflictError,
            ServiceKey,
            ServiceNotFoundError,
            ServiceProvider,
            ServiceProviderContractError,
            ServiceProviderPlan,
            ServiceProviderPlanError,
            ServiceProviderTypeError,
            ServiceRegistrationDisposer,
            ServiceRegistrationToken,
            apply_service_providers,
            plan_service_providers,
        )

        return {
            "DuplicateServiceError": DuplicateServiceError,
            "RuntimeContext": RuntimeContext,
            "RuntimeContextCloseError": RuntimeContextCloseError,
            "RuntimeContextCloseFailure": RuntimeContextCloseFailure,
            "RuntimeContextClosedError": RuntimeContextClosedError,
            "RuntimeContextError": RuntimeContextError,
            "RuntimeContextState": RuntimeContextState,
            "RuntimeIdentity": RuntimeIdentity,
            "ServiceCardinality": ServiceCardinality,
            "ServiceDefinitionConflictError": ServiceDefinitionConflictError,
            "ServiceKey": ServiceKey,
            "ServiceNotFoundError": ServiceNotFoundError,
            "ServiceProvider": ServiceProvider,
            "ServiceProviderContractError": ServiceProviderContractError,
            "ServiceProviderPlan": ServiceProviderPlan,
            "ServiceProviderPlanError": ServiceProviderPlanError,
            "ServiceProviderTypeError": ServiceProviderTypeError,
            "ServiceRegistrationDisposer": ServiceRegistrationDisposer,
            "ServiceRegistrationToken": ServiceRegistrationToken,
            "apply_service_providers": apply_service_providers,
            "plan_service_providers": plan_service_providers,
        }[name]
    if name in {
        "EVENTS",
        "JOBS",
        "MEMORY",
        "RUNTIME_SERVICE_KEYS",
        "PROMPT",
        "SESSION_PROJECTION",
        "TOOLS",
        "RuntimeServiceProvider",
        "RuntimeServiceUnavailableError",
        "create_runtime_context",
    }:
        from .service_providers import (
            EVENTS,
            JOBS,
            MEMORY,
            RUNTIME_SERVICE_KEYS,
            PROMPT,
            SESSION_PROJECTION,
            TOOLS,
            RuntimeServiceProvider,
            RuntimeServiceUnavailableError,
            create_runtime_context,
        )

        return {
            "EVENTS": EVENTS,
            "JOBS": JOBS,
            "MEMORY": MEMORY,
            "RUNTIME_SERVICE_KEYS": RUNTIME_SERVICE_KEYS,
            "PROMPT": PROMPT,
            "SESSION_PROJECTION": SESSION_PROJECTION,
            "TOOLS": TOOLS,
            "RuntimeServiceProvider": RuntimeServiceProvider,
            "RuntimeServiceUnavailableError": RuntimeServiceUnavailableError,
            "create_runtime_context": create_runtime_context,
        }[name]
    if name in {
        "EffectDisposalError",
        "EffectDisposalFailure",
        "EffectScope",
        "EffectScopeClosedError",
        "EffectScopeState",
        "EffectTransaction",
        "EffectTransactionError",
        "EffectTransactionState",
        "EffectTransactionStateError",
    }:
        from .effects import (
            EffectDisposalError,
            EffectDisposalFailure,
            EffectScope,
            EffectScopeClosedError,
            EffectScopeState,
            EffectTransaction,
            EffectTransactionError,
            EffectTransactionState,
            EffectTransactionStateError,
        )

        return {
            "EffectDisposalError": EffectDisposalError,
            "EffectDisposalFailure": EffectDisposalFailure,
            "EffectScope": EffectScope,
            "EffectScopeClosedError": EffectScopeClosedError,
            "EffectScopeState": EffectScopeState,
            "EffectTransaction": EffectTransaction,
            "EffectTransactionError": EffectTransactionError,
            "EffectTransactionState": EffectTransactionState,
            "EffectTransactionStateError": EffectTransactionStateError,
        }[name]
    if name == "RuntimeArtifactManager":
        from .artifacts import RuntimeArtifactManager

        return RuntimeArtifactManager
    if name == "SessionArchiveManager":
        from .session_archive import SessionArchiveManager

        return SessionArchiveManager
    if name == "RuntimeTaskManager":
        from .manager import RuntimeTaskManager

        return RuntimeTaskManager
    if name in {
        "TaskRuntimeStore",
        "task_runtime_id_for_tool_call",
        "task_runtime_status_for_tool_result",
    }:
        from .task_runtime import (
            TaskRuntimeStore,
            task_runtime_id_for_tool_call,
            task_runtime_status_for_tool_result,
        )

        return {
            "TaskRuntimeStore": TaskRuntimeStore,
            "task_runtime_id_for_tool_call": task_runtime_id_for_tool_call,
            "task_runtime_status_for_tool_result": task_runtime_status_for_tool_result,
        }[name]
    if name == "run_preflight_checks":
        from .preflight import run_preflight_checks

        return run_preflight_checks
    if name == "run_postflight_checks":
        from .postflight import run_postflight_checks

        return run_postflight_checks
    if name in {"max_execution_retries", "retry_decision_for_exception"}:
        from .retry import max_execution_retries, retry_decision_for_exception

        return {
            "max_execution_retries": max_execution_retries,
            "retry_decision_for_exception": retry_decision_for_exception,
        }[name]
    if name == "SharedTracker":
        from .shared_tracker import SharedTracker

        return SharedTracker
    if name in {
        "ExecutionProfile",
        "get_execution_profile_char_budget",
        "get_execution_profile_prompt_budget_tokens",
        "get_execution_profile",
        "is_known_execution_profile",
        "list_execution_profiles",
        "normalize_execution_profile",
        "profile_allows_tool",
    }:
        from .execution_profile import (
            ExecutionProfile,
            get_execution_profile_char_budget,
            get_execution_profile_prompt_budget_tokens,
            get_execution_profile,
            is_known_execution_profile,
            list_execution_profiles,
            normalize_execution_profile,
            profile_allows_tool,
        )

        return {
            "ExecutionProfile": ExecutionProfile,
            "get_execution_profile_char_budget": get_execution_profile_char_budget,
            "get_execution_profile_prompt_budget_tokens": get_execution_profile_prompt_budget_tokens,
            "get_execution_profile": get_execution_profile,
            "is_known_execution_profile": is_known_execution_profile,
            "list_execution_profiles": list_execution_profiles,
            "normalize_execution_profile": normalize_execution_profile,
            "profile_allows_tool": profile_allows_tool,
        }[name]
    raise AttributeError(name)
