"""Task module - Background task management"""

from .manager import TaskManager, TaskInfo
from .orchestration import OrchestrationManager
from .state_graph import TaskGraph, TaskNode, TaskStateGraphManager
from .outcome_contract import (
    OutcomeConstraint,
    OutcomeContract,
    OutcomeDeliverable,
    OutcomeSuccessCriterion,
)
from .outcome_planner import OutcomeContractPlanner, OutcomePlan
from .outcome_revision import (
    OutcomeContractRevision,
    OutcomeContractRevisionPlanner,
    invalidate_superseded_message_verifications,
)
from .outcome_schemas import (
    load_outcome_v2_schema,
    load_proof_pack_v1_schema,
    validate_outcome_v2,
    validate_proof_pack_v1,
)

__all__ = [
    "TaskManager",
    "TaskInfo",
    "TaskGraph",
    "TaskNode",
    "TaskStateGraphManager",
    "OrchestrationManager",
    "OutcomeConstraint",
    "OutcomeContract",
    "OutcomeDeliverable",
    "OutcomeSuccessCriterion",
    "OutcomeContractPlanner",
    "OutcomePlan",
    "OutcomeContractRevision",
    "OutcomeContractRevisionPlanner",
    "invalidate_superseded_message_verifications",
    "load_outcome_v2_schema",
    "load_proof_pack_v1_schema",
    "validate_outcome_v2",
    "validate_proof_pack_v1",
]
