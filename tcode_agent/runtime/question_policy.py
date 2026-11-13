"""Explicit, per-run Question policy. Never applies to tool permissions."""

from __future__ import annotations

import re
from typing import Any


def normalize_question_policy(value: str | None) -> str:
    policy = str(value or "ask").strip().lower()
    if policy == "skip":
        policy = "auto-confirm"
    if policy not in {"ask", "auto-confirm"}:
        raise ValueError("question_policy 必须是 ask、auto-confirm 或 skip")
    return policy


def _label(value: str) -> str:
    # Only discard harmless presentation punctuation, not arbitrary qualifiers
    # such as '不', '取消', or a condition attached to a confirmation.
    value = re.sub(r"\s*[（(](?:推荐|recommended)[）)]\s*$", "", value, flags=re.I)
    return re.sub(r"[\s,，。.!！?？:：]", "", value).casefold()


_CONFIRM_LABELS = {
    "确认", "确定", "同意", "继续", "确认继续", "继续执行", "确认执行",
    "确认继续执行", "继续运行", "确认运行", "仍然运行", "仍然继续",
    "是的继续", "是的继续执行", "是继续", "是继续执行",
    "仍然运行这个workflow", "confirm", "continue", "proceed", "ok", "okay",
    "continueanyway", "proceedanyway", "yescontinue", "yesproceed",
}
_YES_LABELS = {"是", "是的", "yes", "y", "true"}


def _asks_to_proceed(question: str) -> bool:
    # Match a whole, simple request to proceed, not a verb anywhere in a factual
    # question such as '是否运行成功?' or in a request for additional data.
    text = question.strip().rstrip("。.!！?？").strip()
    action = r"(?:继续(?:执行|运行)?|执行|运行|开始)"
    target = r"(?:上述计划|当前计划|当前任务|这个任务|该任务|工作流|这个\s*workflow)?"
    return bool(re.fullmatch(
        rf"(?:是否(?:仍然|仍要|要|需要)?|要不要){action}{target}"
        rf"|(?:确认|确定){action}{target}"
        rf"|{action}{target}(?:吗|么)"
        r"|(?:should|shall|may|can) (?:i|we) (?:continue|proceed|run|execute|start)"
        r"|(?:do you want|would you like) (?:me |us )?to (?:continue|proceed)"
        r"|(?:confirm|continue|proceed)",
        text, flags=re.I,
    ))


def confirmation_answer(payload: dict[str, Any]) -> str | None:
    """Select an unambiguous affirmative answer, never a first/default option.

    Template/tool authors may declare the exact confirmation_option for labels
    with application-specific wording. Non-confirmation data must use ordinary
    input fields, not this annotation.
    """
    kind = payload.get("type", "custom")
    options = [str(option) for option in payload.get("options") or []]
    declared = str(payload.get("confirmation_option") or "").strip()
    if declared:
        if kind == "single" and options.count(declared) == 1:
            return declared
        return None
    question_is_confirmation = _asks_to_proceed(str(payload.get("question") or ""))
    if kind == "custom" and not options:
        return "确认" if question_is_confirmation else None
    if kind != "single" or not options:
        return None
    matches = [
        option for option in options
        if _label(option) in _CONFIRM_LABELS
        or (question_is_confirmation and _label(option) in _YES_LABELS)
    ]
    return matches[0] if len(matches) == 1 else None
