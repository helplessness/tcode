"""Built-in tools for TCode Agent"""

from .bash import BashTool
from .exec import ExecTool
from .process import ProcessTool
from .plan import PlanEnterTool, PlanExitTool, PlanValidateTool, PlanReviewTool
from .read import ReadTool
from .inspect_file import InspectFileTool
from .artifact_query import ArtifactQueryTool
from .write import WriteTool
from .edit import EditTool
from .glob import GlobTool
from .grep import GrepTool
from .import_resource import ImportResourceTool
from .task import TaskTool, TaskWaitTool
from .subagent_guide import SubagentGuideTool
from .question import QuestionTool
from .todo import TodoTool
from .memory_write import MemoryWriteTool
from .memory_read import MemoryReadTool
from .memory import MemoryTool
from .memory_cleanup import MemoryCleanupTool
from .session_search import SessionSearchTool
from .result_item_read import ResultItemReadTool
from .context_expand import ContextExpandTool
from .structure_view import StructureViewTool
from .execute_code import ExecuteCodeTool
from .workflow_templates import WorkflowListTemplatesTool
from .workflow_proposal import WorkflowProposalTool
from .workflow_run import WorkflowRunTool
from .skill_run import SkillRunTool
from .skill_install import SkillInstallTool
from .skill_source import SkillSourceTool
from .goal_checkpoint import GoalCheckpointTool
from .scheduled_task import ScheduledTaskTool
from .present_image import PresentImageTool
from .canvas import CanvasTool
from .intelligent_page import IntelligentPageTool
from .research_compare import ResearchCompareTool
from .look_at import LookAtTool
from .evidence_write import EvidenceWriteTool
from .evidence_search import EvidenceSearchTool
from .evidence_graph import EvidenceGraphTool
from .evidence_doctor import EvidenceDoctorTool
from .research_read import ResearchReadTool
from .knowledge_search import KnowledgeSearchTool
from .research_workspace import ResearchWorkspaceTool
from .research_action_commit import ResearchActionCommitTool
from .protein_candidate import ProteinCandidateTool
from .capability_catalog import CapabilityCatalogTool
from .ontology_query import OntologyQueryTool
from .computer import (
    ComputerTool, ComputerDiscoverTool, ComputerObserveTool,
    ComputerElementActionTool, ComputerPointerActionTool,
    ComputerInputActionTool, ComputerControlTool,
    ComputerTypeTextTool, ComputerPressKeyTool, ComputerScrollTool,
)

_OPTIONAL_TOOLS = []
try:
    from .webfetch import WebFetchTool
    _OPTIONAL_TOOLS.append(WebFetchTool)
except Exception:
    WebFetchTool = None  # type: ignore[assignment]

try:
    from .websearch import WebSearchTool
    _OPTIONAL_TOOLS.append(WebSearchTool)
except Exception:
    WebSearchTool = None  # type: ignore[assignment]

try:
    from .browser import BrowserTool
    _OPTIONAL_TOOLS.append(BrowserTool)
except Exception:
    BrowserTool = None  # type: ignore[assignment]


#### exec, process, read, write, edit, 
#### glob, grep, task, webfetch, websearch, 
#### question, todo, memory, skill

# Auto-register all built-in tools
_TOOLS = [
    BashTool,
    ExecTool,
    ProcessTool,
    PlanEnterTool,
    PlanExitTool,
    PlanValidateTool,
    PlanReviewTool,
    ReadTool,
    InspectFileTool,
    ArtifactQueryTool,
    WriteTool,
    EditTool,
    GlobTool,
    GrepTool,
    ImportResourceTool,
    TaskTool,
    TaskWaitTool,
    SubagentGuideTool,
    QuestionTool,
    TodoTool,
    MemoryTool,
    MemoryWriteTool,
    MemoryReadTool,
    MemoryCleanupTool,
    SessionSearchTool,
    ResultItemReadTool,
    ContextExpandTool,
    StructureViewTool,
    ExecuteCodeTool,
    WorkflowListTemplatesTool,
    WorkflowProposalTool,
    WorkflowRunTool,
    SkillRunTool,
    SkillInstallTool,
    SkillSourceTool,
    GoalCheckpointTool,
    ScheduledTaskTool,
    PresentImageTool,
    CanvasTool,
    IntelligentPageTool,
    ResearchCompareTool,
    LookAtTool,
    EvidenceWriteTool,
    EvidenceSearchTool,
    EvidenceGraphTool,
    EvidenceDoctorTool,
    ResearchReadTool,
    KnowledgeSearchTool,
    ResearchWorkspaceTool,
    ResearchActionCommitTool,
    ProteinCandidateTool,
    CapabilityCatalogTool,
    OntologyQueryTool,
    ComputerTool,
    ComputerDiscoverTool,
    ComputerObserveTool,
    ComputerElementActionTool,
    ComputerPointerActionTool,
    ComputerInputActionTool,
    ComputerTypeTextTool,
    ComputerPressKeyTool,
    ComputerScrollTool,
    ComputerControlTool,
] + _OPTIONAL_TOOLS

# Default export - register all tools when imported
for tool_class in _TOOLS:
    tool_class.register()
