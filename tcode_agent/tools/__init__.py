"""Tools module"""

from .base import Tool, ToolContext
from .registry import ToolRegistry

try:
    # Import builtin tools to register them. Keep this best-effort so optional
    # dependencies for a single builtin tool do not break lightweight imports.
    from . import builtin  # noqa: F401
except Exception as exc:  # pragma: no cover
    import warnings

    warnings.warn(f"Built-in tools could not be fully loaded: {exc}")


def _register_skill_tool():
    """Register skill tool after all other tools"""
    from ..skill.tool import SkillTool

    SkillTool.register()


try:
    # Register skill tool
    _register_skill_tool()
except Exception as exc:  # pragma: no cover
    import warnings

    warnings.warn(f"Skill tool could not be loaded: {exc}")


_TEAM_TOOLS_REGISTERED = False
_TEAM_TOOLS_WARNING_EMITTED = False


def ensure_team_tools_registered() -> None:
    """Register TeamMode tools once imports are no longer in the Agent bootstrap path."""
    global _TEAM_TOOLS_REGISTERED, _TEAM_TOOLS_WARNING_EMITTED
    if _TEAM_TOOLS_REGISTERED:
        return
    try:
        from ..team import tools as _  # noqa: F401 - side-effect: registers tools
    except Exception as exc:  # pragma: no cover
        if not _TEAM_TOOLS_WARNING_EMITTED:
            import warnings

            warnings.warn(f"TeamMode tools could not be loaded: {exc}")
            _TEAM_TOOLS_WARNING_EMITTED = True
        return
    _TEAM_TOOLS_REGISTERED = True

__all__ = ["Tool", "ToolContext", "ToolRegistry", "ensure_team_tools_registered"]
