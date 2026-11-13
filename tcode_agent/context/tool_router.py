"""Conservative per-turn tool-definition routing.

Unknown intents keep the complete admitted tool set.  Definitions are reduced
only when the current request has a strong, explainable intent signal.
"""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from typing import Any, Optional

from .builder import latest_user_goal


_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-.]+|[\u4e00-\u9fff]{2,}")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_EXPLICIT_TOOL_RE = re.compile(
    r"(?:\b(?:skill|plugin|mcp|tool)\b|技能|插件|工具)",
    re.IGNORECASE,
)
_BROWSER_REFERENCE_RE = re.compile(
    r"(?P<builtin>(?:tcode(?:'s)?\s*)?(?:built[ -]?in|sandbox(?:ed)?)\s+browser|"
    r"(?:tcode\s*)?(?:内置|沙箱)浏览器|tcode\s*浏览器)|"
    r"(?P<host>(?:本地|本机|系统|桌面)(?:的)?浏览器|(?:电脑|mac)(?:上|里)(?:的)?浏览器|"
    r"(?:local|system|host|desktop|macos)\s+browser|browser\s+on\s+(?:my|the)\s+(?:mac|computer))|"
    r"(?P<named>(?<![a-z0-9])(?:safari|(?:google\s+)?chrome|chromium|firefox|(?:microsoft\s+)?edge)(?![a-z0-9]))",
    re.IGNORECASE,
)
_BROWSER_CLAUSE_RE = re.compile(r"[。！？.!?;；,，\n]|\bbut\b|但(?:是)?|而(?:是)?", re.IGNORECASE)
_BROWSER_NEGATION_RE = re.compile(
    r"不要|别|不用|不使用|不通过|禁止|避免|勿|\b(?:not|never|without|avoid)\b|\bdon['’]t\b",
    re.IGNORECASE,
)
_BROWSER_CHOICE_RE = re.compile(
    r"使用|用|通过|在|打开|启动|切换到|\b(?:use|using|via|in|with|open|launch|activate)\b",
    re.IGNORECASE,
)
_BROWSER_OPERATION_RE = re.compile(r"^\s*(?:浏览器)?(?:中|里)?\s*(?:打开|搜索|访问|浏览|操作|open\b|search\b|browse\b)", re.IGNORECASE)


def browser_selection(goal: str) -> str:
    """Recognize an explicit browser choice, not incidental brand mentions.

    This only filters already-admitted definitions. It does not authorize a
    host browser or turn a sandbox failure into a host fallback. Unspecified
    web interaction defaults to the built-in browser in the agent policy.
    """
    local = builtin = deny_host = False
    for clause in _BROWSER_CLAUSE_RE.split(str(goal or "")):
        for match in _BROWSER_REFERENCE_RE.finditer(clause):
            prefix = clause[max(0, match.start() - 80):match.start()]
            suffix = clause[match.end():]
            negated = bool(_BROWSER_NEGATION_RE.search(prefix))
            if negated:
                deny_host |= match.lastgroup == "host"
                continue
            # "Open Chrome's website" and "search for Safari shortcuts"
            # describe web content, not the host application to operate.
            if match.lastgroup == "named" and re.match(
                r"\s*(?:浏览器)?(?:的|['’]s)?\s*(?:官网|官方网站|网站|文档|website\b|documentation\b|docs\b)",
                suffix, re.IGNORECASE,
            ):
                continue
            chosen = bool(_BROWSER_CHOICE_RE.search(prefix) or _BROWSER_OPERATION_RE.search(suffix))
            if chosen:
                builtin |= match.lastgroup == "builtin"
                local |= match.lastgroup in {"host", "named"}
    if builtin or deny_host:
        return "builtin"
    return "local" if local else "unspecified"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(str(os.environ.get(name, default)).strip()))
    except (TypeError, ValueError):
        return max(minimum, default)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for raw_term in terms:
        term = raw_term.lower()
        if re.search(r"[\u4e00-\u9fff]", term):
            if term in lowered:
                return True
            continue
        # ASCII intent words require lexical boundaries. Substring matching
        # made "latest" accidentally match the code keyword "test".
        escaped = re.escape(term).replace(r"\ ", r"\s+")
        pattern = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
        if re.search(pattern, lowered):
            return True
    return False


_TEXT_TERMS = (
    "直接回答",
    "不用工具",
    "无需工具",
    "不要调用工具",
    "不要使用工具",
    "不要使用任何工具",
    "不使用工具",
    "解释一下",
    "请解释",
    "概念解释",
    "是什么",
    "为什么",
    "润色",
    "改写",
    "翻译",
    "总结以下",
    "闲聊",
    "answer directly",
    "without tools",
    "explain the concept",
    "what is",
    "why is",
    "rewrite",
    "translate",
    "summarize the following",
)
_CODE_TERMS = (
    "代码",
    "实现",
    "修复",
    "重构",
    "仓库",
    "项目",
    "文件",
    "测试",
    "报错",
    "bug",
    "code",
    "implement",
    "fix",
    "refactor",
    "repository",
    "file",
    "files",
    "test",
    "tests",
    "traceback",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".go",
    ".rs",
    ".java",
)
_WEB_TERMS = (
    "搜索",
    "查网页",
    "浏览器",
    "网站",
    "最新",
    "新闻",
    "医疗",
    "法律",
    "法规",
    "政策",
    "价格",
    "股票",
    "汇率",
    "天气",
    "search",
    "browse",
    "website",
    "latest",
    "news",
    "medical",
    "legal",
    "regulation",
    "policy",
    "price",
    "stock",
    "exchange rate",
    "weather",
)
_RESEARCH_TERMS = (
    "论文",
    "文献",
    "研究",
    "证据",
    "引用",
    "pdf",
    "paper",
    "literature",
    "research",
    "evidence",
    "citation",
)
_VISUAL_TERMS = (
    "交互页面",
    "图表",
    "卡片",
    "dashboard",
    "画布",
    "示意图",
    "改色",
    "canvas",
    "intelligent_page",
    "diagram",
    "图片",
    "图像",
    "截图",
    "看图",
    "image",
    "screenshot",
    "visual",
)


def intelligent_ui_intent(goal: str) -> str:
    """Prefer an admitted presentation tool for a natural creation request.

    This is a routing hint, not permission to execute or to invent a tool.
    Existing artifact edits continue to use their selection/state context.
    """
    text = str(goal or "").lower()
    if _contains_any(text, ("只用文字", "仅用文字", "不要画", "不用画", "不要生成",
                            "不要调用工具", "不用工具", "text only", "without tools")):
        return ""
    if not _contains_any(text, ("画", "生成", "制作", "做一", "做个", "做成", "帮我做", "给我", "想要",
                                "create", "draw", "generate", "make", "build")):
        return ""
    # Source-code implementation and browser operation have their own tools.
    if _contains_any(text, ("修改代码", "实现组件", "写代码", "源代码", "浏览器", "打开网页",
                            "react", "solidjs", "browser", "source code")):
        return ""
    if _contains_any(text, ("页面", "界面", "仪表盘", "看板", "图表", "柱状图", "折线图",
                            "dashboard", "interactive page", "chart")):
        return "intelligent_page"
    if _contains_any(text, ("图", "diagram", "illustration")) and _contains_any(
        text, ("点击", "可点", "能点", "点一下", "点开", "点选", "交互", "click", "clickable", "interactive")
    ):
        return "canvas"
    if _contains_any(text, ("图文详解", "图文讲解", "图文解释", "图解", "组成图", "结构示意",
                            "原理图", "机械图", "等轴测插图", "等轴测图", "装配图", "装配教学图", "拆解教学图", "拆解图",
                            "illustrated explanation", "illustrated guide")):
        return "canvas"
    return ""


def knowledge_lookup_intent(goal: str) -> bool:
    text = str(goal or '').lower()
    if _contains_any(text, ('不要检索', '不用检索', '不要搜索', '不查已有', '不要调用工具',
                            '不用工具', 'without tools', 'do not search')):
        return False
    return _contains_any(text, ('？', '?', '什么', '哪些', '为何', '为什么', '如何', '怎么',
                               '能不能', '是否', '有没有', '讲讲', '解释', '介绍', '区别',
                               '作用', '影响', '关系', '查一下', 'what ', 'how ', 'why ', 'explain '))


_DESKTOP_TERMS = (
    "finder",
    "访达",
    "网易云",
    "微信",
    "音乐客户端",
    "桌面应用",
    "本地应用",
    "macos 应用",
    "desktop app",
    "local app",
)
_ORCHESTRATION_TERMS = (
    "委派",
    "协作",
    "成员",
    "团队",
    "子代理",
    "代理团队",
    "工作流",
    "异步任务",
    "定时任务",
    "subagent",
    "agent",
    "delegate",
    "assign_task",
    "list_workers",
    "task_id",
    "worker",
    "workflow",
    "async job",
    "scheduled",
)
_MEMORY_TERMS = (
    "之前的会话",
    "历史会话",
    "记忆",
    "回忆",
    "previous session",
    "history",
    "memory",
    "recall",
)
_MCP_DOMAIN_TERMS = (
    "分子对接",
    "蛋白设计",
    "蛋白质设计",
    "蛋白结构预测",
    "蛋白质结构预测",
    "突变打分",
    "分子动力学",
    "docking",
    "gnina",
    "boltz",
    "ligandmpnn",
    "rfdiffusion",
    "plip",
    "evolvepro",
    "placer",
    "mutation scoring",
    "molecular dynamics",
)

_CATEGORY_TOOL_PATTERNS: dict[str, tuple[str, ...]] = {
    "text": (),
    "code": (
        "inspect_file",
        "read",
        "grep",
        "glob",
        "edit",
        "write",
        "exec",
        "execute_code",
        "process",
        "todo",
        "plan",
        "task",
        "skill",
        "look_at",
        "present_image",
    ),
    "web": (
        "web",
        "browser",
        "fetch",
        "search",
        "import_resource",
        "inspect_file",
        "read",
        "look_at",
        "present_image",
        "skill",
    ),
    "research": (
        "research_compare",
        "result_item_read",
        "artifact_query",
        "ontology_query",
        "research",
        "evidence",
        "paper",
        "web",
        "browser",
        "fetch",
        "search",
        "inspect_file",
        "read",
        "import_resource",
        "look_at",
        "present_image",
        "skill",
    ),
    "visual": ("intelligent_page", "canvas", "look_at", "present_image", "inspect_file", "read", "browser", "skill"),
    "desktop": ("computer",),
    "orchestration": (
        "capability_catalog",
        "task",
        "worker",
        "team",
        "workflow",
        "process",
        "scheduled",
        "todo",
        "plan",
        "question",
        "skill",
    ),
    "memory": ("knowledge_search", "memory", "session_search", "research_read", "research_compare", "result_item_read", "evidence_search", "ontology_query"),
    # Agent-specific MCP admission already enforces the direct/routed server
    # boundary.  Keep the admitted MCP facade visible for natural-language
    # domain requests so users do not have to know internal tool ids.
    "mcp": ("mcp_", "capability_catalog"),
}


@dataclass
class ToolRoutingResult:
    tools: list[Any]
    applied: bool
    categories: list[str]
    original_count: int
    selected_count: int
    reason: str
    estimated_tokens: int = 0
    budget_tokens: int = 0
    budget_exceeded: bool = False

    def telemetry(self) -> dict[str, Any]:
        return {
            "schema": "tcode.tool-definition-routing/v1",
            "applied": self.applied,
            "categories": list(self.categories),
            "original_count": self.original_count,
            "selected_count": self.selected_count,
            "reason": self.reason,
            "estimated_schema_tokens": self.estimated_tokens,
            "schema_budget_tokens": self.budget_tokens,
            "required_tools_exceed_budget": self.budget_exceeded,
            "selected_tools": [
                str(getattr(tool, "name", "") or "") for tool in self.tools
            ],
        }


class ToolDefinitionRouter:
    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        minimum_tools: Optional[int] = None,
        budget_tokens: Optional[int] = None,
    ):
        self.enabled = (
            _env_bool("TCODE_DYNAMIC_TOOL_ROUTING", True)
            if enabled is None
            else bool(enabled)
        )
        self.minimum_tools = (
            _env_int("TCODE_DYNAMIC_TOOL_ROUTING_MIN_TOOLS", 12, minimum=1)
            if minimum_tools is None
            else max(1, int(minimum_tools))
        )
        self.budget_tokens = (_env_int("TCODE_TOOL_SCHEMA_BUDGET_TOKENS", 8000, minimum=1)
                              if budget_tokens is None else max(1, budget_tokens))

    @staticmethod
    def _categories(goal: str) -> list[str]:
        categories: list[str] = []
        has_code = _contains_any(goal, _CODE_TERMS)
        has_web = _contains_any(goal, _WEB_TERMS) or bool(_URL_RE.search(goal))
        has_research = _contains_any(goal, _RESEARCH_TERMS)
        has_visual = _contains_any(goal, _VISUAL_TERMS) or bool(intelligent_ui_intent(goal))
        has_desktop = _contains_any(goal, _DESKTOP_TERMS)
        selection = browser_selection(goal)
        has_web |= selection != "unspecified"
        has_desktop |= selection == "local"
        has_orchestration = _contains_any(goal, _ORCHESTRATION_TERMS)
        has_memory = _contains_any(goal, _MEMORY_TERMS) or knowledge_lookup_intent(goal)
        has_mcp_domain = _contains_any(goal, _MCP_DOMAIN_TERMS)
        if has_code:
            categories.append("code")
        if has_web:
            categories.append("web")
        if has_research:
            categories.append("research")
        if has_visual:
            categories.append("visual")
        if has_desktop:
            categories.append("desktop")
        if has_orchestration:
            categories.append("orchestration")
        if has_memory:
            categories.append("memory")
        if has_mcp_domain:
            categories.append("mcp")
        has_text = _contains_any(goal, _TEXT_TERMS)
        explicitly_tool_free = _contains_any(
            goal,
            (
                "不用工具",
                "无需工具",
                "不要调用工具",
                "不要使用工具",
                "不要使用任何工具",
                "不使用工具",
                "without tools",
            ),
        )
        if explicitly_tool_free and not any(
                (has_web, has_research, has_visual, has_desktop, has_orchestration, has_memory)
            ):
            # Explicit user intent wins over incidental code vocabulary such
            # as “修复后” or a filename mentioned only as conversation context.
            return ["text"]
        requests_named_capability = bool(_EXPLICIT_TOOL_RE.search(goal))
        if (
            has_text
            and not any(
                (has_code, has_web, has_research, has_visual, has_desktop, has_orchestration)
            )
            and (explicitly_tool_free or not requests_named_capability)
        ):
            categories.append("text")
        return categories

    def categories_for(self, *, messages: list[Any], goal: str = "") -> list[str]:
        current_goal = str(goal or latest_user_goal(messages) or "").strip()
        return self._categories(current_goal)

    def should_skip_catalog(self, *, messages: list[Any], goal: str = "") -> bool:
        """Fast path for unambiguous text-only turns.

        This runs before skill and MCP discovery so a plain explanation does
        not pay the latency or schema cost of loading an unused catalog.
        """

        return self.enabled and self.categories_for(messages=messages, goal=goal) == [
            "text"
        ]

    @staticmethod
    def _goal_tokens(goal: str) -> set[str]:
        return {
            token.lower()
            for token in _TOKEN_RE.findall(goal)
            if len(token.strip()) >= 2
        }

    @classmethod
    def _matches_category(cls, tool: Any, patterns: set[str]) -> bool:
        name = str(getattr(tool, "name", "") or "").strip().lower()
        tokens = set(re.split(r"[_\-]+", name))
        return any(pattern == name or pattern in tokens
                   or pattern.endswith("_") and name.startswith(pattern) for pattern in patterns)

    @staticmethod
    def _matches_capability(tool: Any, categories: list[str]) -> bool:
        from ..tools.capabilities import get_tool_capability
        tags = set(get_tool_capability(str(getattr(tool, "name", ""))).tags)
        category_tags = {
            "code": {"local_fs", "shell", "code_execution"},
            "web": {"web"}, "research": {"research", "evidence"},
            "visual": {"vision", "presentation", "structure"},
            "desktop": {"computer"}, "orchestration": {"orchestration", "planning", "team"},
            "memory": {"memory"}, "mcp": {"mcp"},
        }
        return any(tags & category_tags.get(category, set()) for category in categories)

    @staticmethod
    def _definition_tokens(tool: Any) -> int:
        # ponytail: provider-independent estimate; measured provider usage remains authoritative.
        payload = {"name": getattr(tool, "name", ""), "description": getattr(tool, "description", ""),
                   "parameters": getattr(tool, "parameters", {})}
        return max(1, (len(json.dumps(payload, ensure_ascii=False)) + 2) // 3)

    def route(self, tools: list[Any], *, messages: list[Any], goal: str = "",
              activated_tool_ids: list[str] | None = None,
              state_tool_ids: list[str] | None = None) -> ToolRoutingResult:
        required = list(dict.fromkeys([*(activated_tool_ids or []), *(state_tool_ids or [])]))
        result = self._route(tools, messages=messages, goal=goal, activated_tool_ids=required)
        costs = {id(tool): self._definition_tokens(tool) for tool in result.tools}
        result.estimated_tokens = sum(costs.values())
        result.budget_tokens = self.budget_tokens
        if not self.enabled or result.categories == ["text"] or result.estimated_tokens <= self.budget_tokens:
            return result
        protected = set(required) | {"capability_catalog", "skill", "question", "task_wait", "read", "inspect_file"}
        current_goal = goal or latest_user_goal(messages)
        preferred_ui_tool = intelligent_ui_intent(current_goal)
        if preferred_ui_tool:
            protected.add(preferred_ui_tool)
        if knowledge_lookup_intent(current_goal):
            protected.add('knowledge_search')
        normalized_goal = " " + re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", current_goal.lower()).strip() + " "
        protected.update(tool.name for tool in result.tools if
                         " " + re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", tool.name.lower()).strip() + " " in normalized_goal)
        selected = [tool for tool in result.tools if getattr(tool, "name", "") in protected]
        total = sum(costs[id(tool)] for tool in selected)
        selected_ids = {id(tool) for tool in selected}
        for tool in result.tools:
            if id(tool) not in selected_ids and total + costs[id(tool)] <= self.budget_tokens:
                selected.append(tool)
                selected_ids.add(id(tool))
                total += costs[id(tool)]
        result.tools = [tool for tool in result.tools if id(tool) in selected_ids]
        result.selected_count = len(result.tools)
        result.applied = True
        result.reason += ":schema_budget"
        result.estimated_tokens = total
        result.budget_exceeded = total > self.budget_tokens
        return result

    @classmethod
    def _matches_named_domain_tool(
        cls,
        tool: Any,
        goal_tokens: set[str],
        *,
        goal: str = "",
    ) -> bool:
        name = str(getattr(tool, "name", "") or "").strip().lower()
        if not name or not goal_tokens:
            return False
        # Preserve tools that the user names explicitly.  MCP ids contain a
        # mixture of underscores and hyphens (for example
        # ``mcp_job-protocol-mcp_submit_job``), while the generic token matcher
        # below splits those separators.  Comparing a separator-normalized
        # phrase first prevents dynamic routing from hiding the exact tool the
        # request asks the model to call.
        normalized_name = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", name).strip()
        normalized_goal = re.sub(
            r"[^a-z0-9\u4e00-\u9fff]+", " ", str(goal or "").lower()
        ).strip()
        if normalized_name and normalized_name in normalized_goal:
            return True
        name_tokens = {
            token.lower() for token in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", name) if token
        }
        return bool(name_tokens & goal_tokens)

    def _route(
        self,
        tools: list[Any],
        *,
        messages: list[Any],
        goal: str = "",
        activated_tool_ids: list[str] | None = None,
    ) -> ToolRoutingResult:
        admitted = list(tools or [])
        if not self.enabled:
            return ToolRoutingResult(
                admitted, False, [], len(admitted), len(admitted), "disabled"
            )
        if len(admitted) < self.minimum_tools:
            return ToolRoutingResult(
                admitted,
                False,
                [],
                len(admitted),
                len(admitted),
                "below_minimum_tool_count",
            )
        current_goal = str(goal or latest_user_goal(messages) or "").strip()
        categories = self.categories_for(messages=messages, goal=current_goal)
        if not categories:
            return ToolRoutingResult(
                admitted,
                False,
                [],
                len(admitted),
                len(admitted),
                "unknown_intent_keeps_full_catalog",
            )
        patterns = {
            pattern
            for category in categories
            for pattern in _CATEGORY_TOOL_PATTERNS.get(category, ())
        }
        goal_tokens = self._goal_tokens(current_goal)
        retained = set(activated_tool_ids or []) | {"capability_catalog"}
        selected = [
            tool
            for tool in admitted
            if categories != ["text"] and str(getattr(tool, "name", "")) in retained
            or str(getattr(tool, "name", "")) == "context_expand" and categories != ["text"]
            or "web" in categories and str(getattr(tool, "name", "")) in {"task", "task_wait"}
            or self._matches_category(tool, patterns)
            or self._matches_capability(tool, categories)
            or self._matches_named_domain_tool(tool, goal_tokens, goal=current_goal)
        ]
        if not selected and categories != ["text"]:
            return ToolRoutingResult(
                admitted,
                False,
                categories,
                len(admitted),
                len(admitted),
                "no_safe_match_keeps_full_catalog",
            )
        return ToolRoutingResult(
            selected,
            True,
            categories,
            len(admitted),
            len(selected),
            "strong_intent_signal",
        )
