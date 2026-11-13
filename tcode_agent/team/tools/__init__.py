"""Team tools package - registers team management tools"""

from .create_worker import CreateWorkerTool
from .assign_task import AssignTaskTool
from .list_teams import ListTeamsTool
from .list_workers import ListWorkersTool
from .terminate_worker import TerminateWorkerTool
from .team_memory import TeamMemoryWriteTool, TeamMemoryReadTool
from .task_graph import TaskGraphReadTool
from .ask_peer_question import AskPeerQuestionTool
from .answer_peer_question import AnswerPeerQuestionTool

_TOOLS = [
    CreateWorkerTool,
    AssignTaskTool,
    ListTeamsTool,
    ListWorkersTool,
    TerminateWorkerTool,
    TeamMemoryWriteTool,
    TeamMemoryReadTool,
    TaskGraphReadTool,
    AskPeerQuestionTool,
    AnswerPeerQuestionTool,
]

for _tool_cls in _TOOLS:
    _tool_cls.register()
