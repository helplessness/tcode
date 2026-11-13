"""Skill module - Specialized skills for specific tasks"""

from .manager import (
    SkillAction,
    SkillActionArgument,
    SkillDuplicate,
    SkillInfo,
    SkillManager,
)


def __getattr__(name: str):
    if name == "SkillTool":
        from .tool import SkillTool

        return SkillTool
    if name in {"CompositeSkillPlan", "CompositeSkillStep", "SkillCompositePlanner"}:
        from .composite import CompositeSkillPlan, CompositeSkillStep, SkillCompositePlanner

        return {
            "CompositeSkillPlan": CompositeSkillPlan,
            "CompositeSkillStep": CompositeSkillStep,
            "SkillCompositePlanner": SkillCompositePlanner,
        }[name]
    raise AttributeError(name)

__all__ = [
    "SkillAction",
    "SkillActionArgument",
    "SkillDuplicate",
    "SkillManager",
    "SkillInfo",
    "SkillTool",
    "CompositeSkillPlan",
    "CompositeSkillStep",
    "SkillCompositePlanner",
]
