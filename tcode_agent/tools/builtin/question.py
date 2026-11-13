"""Question tool - Ask user for input

Allows the agent to ask questions and get user responses.
Uses Python's input() for CLI interaction.
"""

import asyncio
import json
from enum import Enum
from typing import Any, Optional, Literal

import pydantic

from ..base import Tool, ToolContext, ParameterSchema
from ...types import ToolResult
from ...runtime.question_policy import confirmation_answer, normalize_question_policy


class QuestionType(str, Enum):
    """Type of question"""

    SINGLE_CHOICE = "single"  # Single select
    MULTIPLE_CHOICE = "multiple"  # Multi-select
    CUSTOM = "custom"  # Free text input


class BatchQuestionItem(ParameterSchema):
    """One question in a compact multi-question request."""

    id: str = pydantic.Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    question: str = pydantic.Field(min_length=1, max_length=2000)
    header: str | None = pydantic.Field(default=None, max_length=80)
    type: QuestionType = QuestionType.CUSTOM
    options: list[str] | None = None
    allow_custom: bool = True
    required: bool = True
    recommendation: str | None = pydantic.Field(default=None, max_length=500)

    @pydantic.field_validator("options", mode="before")
    @classmethod
    def _coerce_options(cls, value: Any) -> Any:
        return QuestionParameters._coerce_options(value)


class QuestionParameters(ParameterSchema):
    """Parameters for question tool"""

    question: str = ""
    questions: list[BatchQuestionItem] | None = pydantic.Field(default=None, min_length=1)
    type: QuestionType = QuestionType.CUSTOM
    options: list[str] | None = None
    allow_custom: bool = True  # Allow custom answer for single/multiple choice
    default_answer: str | None = None
    timeout_seconds: int = 300
    goal: str | None = None
    uncertainties: list[str] | None = None
    known_context: str | None = None
    required: bool = True
    preference: Literal['language', 'verbosity', 'format'] | None = pydantic.Field(
        default=None, description='Goal mode only: optional report presentation preference. Only exact host-defined question templates in the goal instructions are eligible for timeout defaults. Arbitrary wording, scientific input, file input format, permissions and confirmations never default; required=true never expires.')
    recommendation: str | None = None
    confirmation_option: str | None = pydantic.Field(
        default=None,
        description=(
            "Exact affirmative option for a single-choice proceed/cancel confirmation. "
            "Must be in options. Do not use for factual answers, missing input, "
            "tool permissions, or result acceptance."
        ),
    )
    metadata: dict[str, Any] | None = None

    @pydantic.model_validator(mode="after")
    def _validate_question_shape(self):
        if not self.question.strip() and not self.questions and not self.uncertainties:
            raise ValueError("question、questions 或 uncertainties 至少提供一项")
        ids = [item.id for item in self.questions or []]
        if len(ids) != len(set(ids)):
            raise ValueError("questions 中的 id 必须唯一")
        return self

    @pydantic.field_validator("options", mode="before")
    @classmethod
    def _coerce_options(cls, value: Any) -> Any:
        if value is None or isinstance(value, list):
            return value

        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return [str(item) for item in parsed]
                except Exception:
                    pass
            return [item.strip() for item in text.split(",") if item.strip()]

        return value


class QuestionTool(Tool):
    """Ask user questions and get responses"""

    @property
    def id(self) -> str:
        return "question"

    @property
    def description(self) -> str:
        return """Ask the user a question and wait for their response.

Supports three types:
1. **single** - Single choice selection (pick one option)
2. **multiple** - Multiple choice selection (pick multiple options)  
3. **custom** - Free text input (default)

For multiple independent clarifications, prefer one call with `questions`. Each item
requires a stable `id`, `question`, and its own type/options. The user sees the
items together and submits them once. Keep `question` as an optional batch title.
Do not pack unrelated questions into one free-text string.

Use this when missing input or a blocking choice prevents further task execution.
In goal mode the host retains the question without blocking independent authorized
work. Required inputs never expire. Only optional language/verbosity/format
preferences with a valid default may time out; unanswered questions prevent final
acceptance. A late answer guides subsequent work, never grants tool permission.
Do not use this tool to ask whether a completed result is accepted, satisfactory,
or ready for archival. Deliver the candidate response first; the host runs outcome
validation and, when needed, offers confirmation in the result's 综合评分 card.
A Question answer is not confirmation of that result card. Tool approvals still
belong to the separate Human Review mechanism."""

    @property
    def parameters(self) -> type:
        return QuestionParameters

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = QuestionParameters(**args)

        batch = parsed.questions or []
        question = parsed.question.strip() or (f"请一次回答以下 {len(batch)} 个问题" if batch else "")
        q_type = parsed.type
        options = parsed.options or []
        allow_custom = parsed.allow_custom
        default_answer = parsed.default_answer
        timeout_seconds = parsed.timeout_seconds
        goal = (parsed.goal or "").strip()
        uncertainties = [str(item).strip() for item in (parsed.uncertainties or []) if str(item).strip()]
        known_context = (parsed.known_context or "").strip()
        recommendation = (parsed.recommendation or "").strip()
        metadata = dict(parsed.metadata or {})
        request_required = any(item.required for item in batch) if batch else parsed.required
        # This policy comes only from the host runtime, never from model arguments.
        policy = normalize_question_policy((context.extra or {}).get("__question_policy__"))
        if callable((context.extra or {}).get("__goal_checkpoint__")):
            # A goal's missing facts must not be filled by auto-confirm defaults.
            policy = "ask"

        should_ask = self._should_ask_user(
            question=question,
            required=request_required,
            default_answer=default_answer,
            uncertainties=uncertainties,
            known_context=known_context,
        )
        goal_question = (context.extra or {}).get('__goal_question__')
        if not should_ask and policy == "ask" and not callable(goal_question):
            answer = recommendation or default_answer or ""
            return ToolResult(
                title="User Response",
                output=f"Skipped question; using inferred answer: {answer}" if answer else "Skipped question; enough context available.",
                metadata={
                    "type": "smart_clarification",
                    "skipped": True,
                    "answer": answer,
                    "goal": goal,
                    "uncertainties": uncertainties,
                },
            )

        if uncertainties and not question.strip():
            question = self._merge_uncertainties(goal=goal, uncertainties=uncertainties, recommendation=recommendation)

        # Format the question display
        if batch:
            display = self._format_batch_questions(question, batch)
        elif q_type == QuestionType.SINGLE_CHOICE and options:
            display = self._format_single_choice(question, options, allow_custom)
        elif q_type == QuestionType.MULTIPLE_CHOICE and options:
            display = self._format_multiple_choice(question, options, allow_custom)
        else:
            display = self._format_custom_question(question)

        if recommendation:
            display = display + f"\n\nRecommended: {recommendation}"

        user_answer = ""
        interactive_callback_used = False

        callback_payload = {
            "question": question,
            "type": "batch" if batch else (q_type.value if isinstance(q_type, QuestionType) else str(q_type)),
            "options": options,
            "allow_custom": allow_custom,
            "display": display,
            "default_answer": default_answer,
            "timeout_seconds": timeout_seconds,
            "goal": goal,
            "uncertainties": uncertainties,
            "known_context": known_context,
            "recommendation": recommendation,
            "confirmation_option": parsed.confirmation_option,
            "required": request_required,
            "preference": parsed.preference,
            "metadata": metadata,
        }
        if batch:
            callback_payload["questions"] = [item.model_dump(mode="json") for item in batch]

        if callable(goal_question):
            return await goal_question(callback_payload, context.call_id)

        auto_answer = confirmation_answer(callback_payload) if policy == "auto-confirm" else None
        if auto_answer is not None:
            user_answer = auto_answer

        resolver = (context.extra or {}).get("__question_resolve__") if context.extra else None
        if resolver and not user_answer:
            resolved_answer = resolver(callback_payload)
            if hasattr(resolved_answer, "__await__"):
                resolved_answer = await resolved_answer
            if resolved_answer is not None:
                user_answer = str(resolved_answer).strip()

        # Prefer frontend callback when available
        callback = (context.extra or {}).get("__question_ask__") if context.extra else None
        if policy == "auto-confirm":
            # An unresolved required input still returns a checkpoint, but never
            # opens a terminal/web prompt or falls back to blocking input().
            interactive_callback_used = True
        elif callback and not user_answer:
            interactive_callback_used = True
            callback_result = callback(callback_payload)
            if hasattr(callback_result, "__await__"):
                callback_result = await callback_result
            user_answer = "" if callback_result is None else str(callback_result).strip()
        elif not user_answer:
            # Fallback to CLI input
            print(f"\n{'=' * 60}")
            print(f"❓ {display}")
            print(f"{'=' * 60}\n")

            loop = asyncio.get_event_loop()
            user_answer = await loop.run_in_executor(None, self._get_input, "Your answer: ")

        if request_required and interactive_callback_used and not user_answer:
            return ToolResult(
                title="Awaiting User Input",
                output=(
                    ("自动确认模式无法为此问题选择明确的确认答案，请补充具体输入后重试。\n"
                     if policy == "auto-confirm" else "当前子任务正在等待用户输入。\n")
                    + f"- question: {question}\n"
                    "- next_action: 由父会话获取回答后恢复原任务。"
                ),
                metadata={
                    "type": "question_waiting",
                    "awaiting_input": True,
                    "waiting_for_user": True,
                    "question_payload": callback_payload,
                    "answer": "",
                    "goal": goal,
                    "uncertainties": uncertainties,
                    "skipped": False,
                    "retryable": True,
                    "should_end_turn": True,
                    "termination_scope": "agent",
                    "question_policy": policy,
                    "auto_confirmed": False,
                },
            )

        # Process the answer
        if batch:
            result = self._process_batch_answer(user_answer, batch)
        elif q_type == QuestionType.SINGLE_CHOICE and options:
            result = self._process_single_choice(user_answer, options, allow_custom)
        elif q_type == QuestionType.MULTIPLE_CHOICE and options:
            result = self._process_multiple_choice(user_answer, options, allow_custom)
        else:
            result = self._process_custom_answer(user_answer)

        return ToolResult(
            title="Automatically Confirmed" if auto_answer is not None else "User Response",
            output=(f"按显式批处理策略自动确认：{user_answer}（非用户现场回答）。"
                    if auto_answer is not None else result["formatted"]),
            metadata={
                **result, "goal": goal, "uncertainties": uncertainties,
                "skipped": policy == "auto-confirm" and not user_answer,
                "metadata": metadata,
                "question_policy": policy,
                "auto_confirmed": auto_answer is not None,
                **({"answer_source": "auto_confirmation", "interaction_skipped": True}
                   if auto_answer is not None else {}),
            },
        )

    def _should_ask_user(
        self,
        *,
        question: str,
        required: bool,
        default_answer: Optional[str],
        uncertainties: list[str],
        known_context: str,
    ) -> bool:
        if required:
            return True
        if not question.strip() and not uncertainties:
            return False
        if default_answer and len(uncertainties) <= 1 and known_context:
            return False
        return True

    def _merge_uncertainties(
        self,
        *,
        goal: str,
        uncertainties: list[str],
        recommendation: str,
    ) -> str:
        lines = []
        if goal:
            lines.append(f"为了更稳定地完成目标：{goal}")
        lines.append("我还缺少以下关键信息，请尽量一次说明：")
        for idx, item in enumerate(uncertainties, 1):
            lines.append(f"{idx}. {item}")
        if recommendation:
            lines.append(f"如果你没有强偏好，建议：{recommendation}")
        return "\n".join(lines)

    def _format_single_choice(
        self, question: str, options: list[str], allow_custom: bool
    ) -> str:
        """Format single choice question"""
        lines = [
            question,
            "",
            "Options (select one):",
        ]

        for i, option in enumerate(options, 1):
            lines.append(f"  {i}. {option}")

        if allow_custom:
            lines.append(f"  Or type your own answer")

        lines.append("")
        lines.append("Enter the number of your choice, or type your answer")

        return "\n".join(lines)

    def _format_multiple_choice(
        self, question: str, options: list[str], allow_custom: bool
    ) -> str:
        """Format multiple choice question"""
        lines = [
            question,
            "",
            "Options (select multiple, comma-separated):",
        ]

        for i, option in enumerate(options, 1):
            lines.append(f"  {i}. {option}")

        if allow_custom:
            lines.append(f"  Or type your own answer")

        lines.append("")
        lines.append("Example: 1,3,5 or type your own answer")

        return "\n".join(lines)

    def _format_custom_question(self, question: str) -> str:
        """Format custom question"""
        return f"{question}\n\nType your answer below:"

    def _format_batch_questions(self, title: str, questions: list[BatchQuestionItem]) -> str:
        lines = [title]
        for index, item in enumerate(questions, 1):
            lines.append(f"{index}. {item.header + ': ' if item.header else ''}{item.question}")
        return "\n".join(lines)

    def _process_batch_answer(self, answer: str, questions: list[BatchQuestionItem]) -> dict[str, Any]:
        try:
            raw = json.loads(answer)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("批量问题回答必须是按问题 id 索引的 JSON 对象") from exc
        if not isinstance(raw, dict):
            raise ValueError("批量问题回答必须是 JSON 对象")

        answers: dict[str, str] = {}
        items: list[dict[str, Any]] = []
        for item in questions:
            value = str(raw.get(item.id) or "").strip()
            if item.required and not value:
                raise ValueError(f"问题 {item.id} 需要回答")
            if item.type == QuestionType.SINGLE_CHOICE and item.options:
                processed = self._process_single_choice(value, item.options, item.allow_custom)
                normalized = str(processed.get("selected_option") or "")
                if value and not normalized:
                    raise ValueError(f"问题 {item.id} 的选项无效")
            elif item.type == QuestionType.MULTIPLE_CHOICE and item.options:
                if value and not item.allow_custom:
                    valid = {str(index) for index in range(1, len(item.options) + 1)}
                    valid.update(option.strip().casefold() for option in item.options)
                    parts = [part.strip() for part in value.replace("，", ",").split(",")]
                    if any(part not in valid and part.casefold() not in valid for part in parts):
                        raise ValueError(f"问题 {item.id} 的选项无效")
                processed = self._process_multiple_choice(value, item.options, item.allow_custom)
                selected = processed.get("selected_options") or []
                normalized = json.dumps(selected, ensure_ascii=False)
                if value and not selected:
                    raise ValueError(f"问题 {item.id} 的选项无效")
            else:
                processed = self._process_custom_answer(value)
                normalized = value
            answers[item.id] = normalized
            items.append({"id": item.id, **processed})

        encoded = json.dumps(answers, ensure_ascii=False)
        return {
            "type": "batch_questions",
            "answer": encoded,
            "answers": answers,
            "items": items,
            "formatted": "User answered: " + "; ".join(
                f"{item.id}={answers[item.id]}" for item in questions
            ),
        }

    def _get_input(self, prompt_text: str) -> str:
        """Get user input using input()"""
        try:
            return input(prompt_text).strip()
        except EOFError:
            return ""
        except KeyboardInterrupt:
            print("\n\n⚠️  Question cancelled by user")
            return "[cancelled]"

    def _process_single_choice(
        self, answer: str, options: list[str], allow_custom: bool
    ) -> dict:
        """Process single choice answer"""
        result = {
            "type": "single_choice",
            "answer": answer,
            "selected_option": None,
            "selected_index": None,
        }

        # Check if it's a number
        if answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(options):
                result["selected_option"] = options[idx]
                result["selected_index"] = idx
                result["formatted"] = f"User selected: {options[idx]}"
                return result

        # Check direct text match
        for idx, option in enumerate(options):
            if answer.strip().lower() == option.strip().lower():
                result["selected_option"] = option
                result["selected_index"] = idx
                result["formatted"] = f"User selected: {option}"
                return result

        # Custom answer
        if allow_custom:
            result["selected_option"] = answer
            result["formatted"] = f"User custom answer: {answer}"
        else:
            result["formatted"] = f"Invalid selection. Options: {', '.join(options)}"

        return result

    def _process_multiple_choice(
        self, answer: str, options: list[str], allow_custom: bool
    ) -> dict:
        """Process multiple choice answer"""
        result = {
            "type": "multiple_choice",
            "answer": answer,
            "selected_options": [],
            "selected_indices": [],
        }

        # Parse comma-separated numbers
        parts = [p.strip() for p in answer.split(",")]
        valid_selections = []

        for part in parts:
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(options):
                    valid_selections.append(options[idx])
                    result["selected_indices"].append(idx)
                continue

            for idx, option in enumerate(options):
                if part.lower() == option.strip().lower():
                    valid_selections.append(option)
                    result["selected_indices"].append(idx)
                    break

        if valid_selections:
            result["selected_options"] = valid_selections
            result["formatted"] = f"User selected: {', '.join(valid_selections)}"
        elif allow_custom and answer:
            result["selected_options"] = [answer]
            result["formatted"] = f"User custom answer: {answer}"
        else:
            result["formatted"] = f"No valid selections. Options: {', '.join(options)}"

        return result

    def _process_custom_answer(self, answer: str) -> dict:
        """Process custom text answer"""
        return {
            "type": "custom",
            "answer": answer,
            "formatted": f"User answered: {answer}"
            if answer
            else "User provided no answer",
        }

    @classmethod
    def register(cls):
        from ..registry import ToolRegistry

        ToolRegistry.register(cls())
