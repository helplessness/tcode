"""Turn-level verification planning.

The planner has one deliberately small job: decide whether a turn needs formal
verification and compile the corresponding :class:`OutcomeContract`.  It never
executes the task and it never decides whether the result passed.

Planning is conservative.  Explicit workflow/API contracts always win.  Automatic
planning is limited to facts that the host can identify without asking a model to
guess: structured output requirements and explicit workspace file mutation targets.
Everything else falls back to a non-enforcing legacy contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..providers.base import ChatMessage
from ..runtime.async_completion import async_continuation_identifier
from ..runtime.verification import verification_chat
from .outcome_contract import OutcomeContract


_MUTATION_WORDS = re.compile(
    r"(?:修改|更新|编辑|重写|写入|修正|写(?=\s|`|到|成|入)|创建|新建|生成|保存|落地|实现|修复|"
    r"(?<![A-Za-z0-9_])(?:modify|update|edit|rewrite|write|create|generate|save|"
    r"implement|fix)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)]+)\)")
_CODE_SPAN = re.compile(r"`(?P<target>[^`]+)`")
_ATTACHMENT_BLOCK = re.compile(
    r"(?mi)^[ \t]*#{1,6}[ \t]+File:[^\r\n]*"
    r"(?:\r?\n[ \t]*Path:[^\r\n]*)?"
)
_RESOURCE_CONTEXT_BLOCK = re.compile(r"(?ms)^# Resource Context\n.*?(?=^# |\Z)")
_BARE_FILE = re.compile(
    r"(?P<target>(?:\.\.?/|/)?(?:[^\s，、。；;：:()\[\]{}<>]+/)*"
    r"[^\s，、。；;：:()\[\]{}<>/]+\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}"
    r"(?::\d+(?::\d+)?)?)(?![A-Za-z0-9_.-])"
)
_NEGATED_MUTATION = re.compile(r"(?:不要|不能|不得|无需|不必|禁止|勿|do\s+not\b|don't\b|without\b)[^。；;！!？?\n]{0,48}$", re.IGNORECASE)
_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_INPUT_WORDS = re.compile(r"输入|只读|读取|阅读|读回|核对|对照|打开|(?<![A-Za-z])(?:read|inspect|open)(?![A-Za-z])", re.IGNORECASE)
_COMPLEX_TASK_WORDS = re.compile(
    r"(?:调研|研究|分析|比较|评估|规划|设计|实现|开发|修复|排查|测试|运行|执行|"
    r"生成报告|生成方案|工作流|workflow|research|analy[sz]e|compare|evaluate|plan|"
    r"design|implement|develop|debug|test|execute|investigate)",
    re.IGNORECASE,
)
_HIGH_RISK_WORDS = re.compile(
    r"(?:删除|清空|覆盖|发布|部署|发送|付款|转账|授权|生产环境|不可逆|"
    r"delete|purge|overwrite|publish|deploy|send|payment|transfer|authorize|production)",
    re.IGNORECASE,
)
_WORKFLOW_WORDS = re.compile(r"(?:workflow|工作流|流程模板|子任务编排)", re.IGNORECASE)
_CLEAR_SIMPLE_CHAT = re.compile(
    r"^(?:你好|您好|嗨|谢谢|感谢|再见|解释|介绍|什么是|为什么|为何|如何|怎么|怎样|"
    r"hello|hi|thanks|thank\s+you|bye|explain|what\s+is|why|how\s+to)",
    re.IGNORECASE,
)
_LEVEL_RANK = {"V0": 0, "V1": 1, "V2": 2, "V3": 3}


@dataclass(frozen=True, slots=True)
class OutcomePlan:
    """Explainable result of planning one turn."""

    contract: OutcomeContract
    level: str
    source: str
    reason_code: str
    formal_verification: bool
    confidence: float
    targets: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "source": self.source,
            "reason_code": self.reason_code,
            "formal_verification": self.formal_verification,
            "confidence": self.confidence,
            "targets": list(self.targets),
            "contract_id": self.contract.contract_id,
            "contract_version": self.contract.version,
        }


class OutcomeContractPlanner:
    """Build the verification contract before the Agent starts executing."""

    @classmethod
    def plan(
        cls,
        user_message: str,
        *,
        output_format: Optional[dict[str, Any]] = None,
        supplied: Optional[dict[str, Any]] = None,
        workspace_root: Optional[str] = None,
    ) -> OutcomePlan:
        explicit = dict(supplied or {}) if isinstance(supplied, dict) else {}
        if explicit:
            contract = OutcomeContract.compile(
                user_message,
                output_format=output_format,
                supplied=explicit,
            )
            return OutcomePlan(
                contract=contract,
                level=cls._explicit_level(contract),
                source="explicit",
                reason_code="explicit_contract",
                formal_verification=contract.compatibility_mode == "native_v2",
                confidence=1.0,
                targets=cls._contract_targets(contract),
            )

        structured = cls._structured_output_contract(user_message, output_format)
        if structured is not None:
            return structured

        targets = cls._mutation_targets(user_message, workspace_root)
        if targets:
            baselines = [
                cls._artifact_baseline(path, workspace_root)
                for path in targets
            ]
            content_review = cls._host_minimum_level(user_message) != "V0" or bool(
                re.search(r"审查|复核|\breview\b|\baudit\b", user_message, re.IGNORECASE)
            )
            contract = OutcomeContract.compile(
                user_message,
                output_format=output_format,
                supplied={
                    "schema_version": "tcode.outcome/v2",
                    "required_proof_level": "P2",
                    "semantic_validation": "always",
                    "max_repair_attempts": 2,
                    "success_criteria": [
                        {
                            "description": (
                                "Every explicitly requested workspace file was created "
                                "or changed during this turn."
                            ),
                            "verifier_refs": ["artifact_changed"],
                            "expected_artifacts": [
                                {"path": path, "required": True} for path in targets
                            ],
                            "parameters": {"artifact_baselines": baselines},
                        }
                    ] + ([{
                        "description": "The saved file content satisfies the original goal, evidence, scope and review requirements; file existence alone is not content acceptance.",
                        "verifier_refs": ["semantic"],
                        "expected_artifacts": [{"path": path, "required": True} for path in targets],
                    }] if content_review else []),
                    "metadata": {
                        "planning": {
                            "source": "automatic",
                            "reason_code": "explicit_file_mutation_target",
                            "level": "V2",
                            "targets": targets,
                        }
                    },
                },
            )
            return OutcomePlan(
                contract=contract,
                level="V2",
                source="automatic",
                reason_code="explicit_file_mutation_target",
                formal_verification=True,
                confidence=0.95,
                targets=tuple(targets),
            )

        contract = OutcomeContract.compile(
            user_message,
            output_format=output_format,
        )
        return OutcomePlan(
            contract=contract,
            level="V0",
            source="fallback",
            reason_code="no_objective_verification_target",
            formal_verification=False,
            confidence=1.0,
        )

    @classmethod
    async def plan_turn(
        cls,
        user_message: str,
        *,
        output_format: Optional[dict[str, Any]] = None,
        supplied: Optional[dict[str, Any]] = None,
        workspace_root: Optional[str] = None,
        provider: Any = None,
        verification_enabled: bool = True,
        usage_session: Any = None,
    ) -> OutcomePlan:
        """Plan one turn, using a bounded planner Agent only for complex free text."""

        if not verification_enabled:
            return OutcomePlan(
                contract=OutcomeContract.compile(user_message, output_format=output_format, supplied=supplied),
                level="V0", source="user_preference", reason_code="result_verification_disabled",
                formal_verification=False, confidence=1.0,
            )

        if cls._uses_injected_async_result(user_message):
            contract = OutcomeContract.compile(
                user_message,
                supplied={
                    "schema_version": "tcode.outcome/v2",
                    "required_proof_level": "P1",
                    "risk_level": "low",
                    "semantic_validation": "never",
                    "max_repair_attempts": 0,
                    "success_criteria": [
                        {
                            "description": (
                                "The host-recorded asynchronous job completed and its "
                                "returned result passed validation."
                            ),
                            "required": True,
                            "verifier_refs": ["semantic"],
                        }
                    ],
                    "metadata": {
                        "planning": {
                            "source": "automatic",
                            "reason_code": "async_result_continuation",
                            "level": "V1",
                            "requires_trusted_async_receipt": True,
                        }
                    },
                },
            )
            return OutcomePlan(
                contract=contract,
                level="V1",
                source="automatic",
                reason_code="async_result_continuation",
                formal_verification=True,
                confidence=1.0,
            )

        deterministic = cls.plan(
            user_message,
            output_format=output_format,
            supplied=supplied,
            workspace_root=workspace_root,
        )
        if deterministic.source != "fallback":
            return deterministic

        minimum_level = cls._host_minimum_level(user_message)
        if minimum_level == "V0" and cls._clearly_simple_chat(user_message):
            return deterministic

        decision = (
            await cls._ask_planner_agent(user_message, provider=provider, usage_session=usage_session)
            if cls._agent_planner_enabled()
            else {}
        )
        if not decision and minimum_level == "V0":
            return deterministic
        return cls._compile_agent_decision(
            user_message,
            decision=decision,
            minimum_level=minimum_level,
        )

    @staticmethod
    def _host_minimum_level(user_message: str) -> str:
        text = str(user_message or "")
        if OutcomeContractPlanner._clearly_simple_chat(text):
            return "V0"
        if _HIGH_RISK_WORDS.search(text) or _WORKFLOW_WORDS.search(text):
            return "V3"
        if _COMPLEX_TASK_WORDS.search(text):
            return "V2"
        return "V0"

    @staticmethod
    def _clearly_simple_chat(user_message: str) -> bool:
        return bool(_CLEAR_SIMPLE_CHAT.search(str(user_message or "").strip()))

    @staticmethod
    def _agent_planner_enabled() -> bool:
        value = str(
            os.environ.get("TCODE_OUTCOME_INTENT_PLANNER_ENABLED", "0") or "0"
        ).strip().lower()
        return value not in {"0", "false", "no", "off"}

    @classmethod
    async def _ask_planner_agent(
        cls,
        user_message: str,
        *,
        provider: Any,
        usage_session: Any = None,
    ) -> dict[str, Any]:
        if provider is None or not callable(getattr(provider, "chat", None)):
            return {}
        prompt = (
            "Classify the user's task for result verification. Do not execute the task. "
            "Return exactly one JSON object with: level (V0|V1|V2|V3), confidence "
            "(0..1), risk_level (low|medium|high|critical), requires_tool_evidence "
            "(boolean), reason_code (short string), and success_criteria (array of 1-6 "
            "plain-language requirements taken only from the user's intent). V0 is only "
            "for simple conversational answers with no tools or persistent effects; V1 "
            "is a bounded format/read check; V2 is a multi-step, tool, artifact, research, "
            "or delegated deliverable; V3 is workflow, irreversible, external-write, or "
            "high-consequence work. If acceptance details are missing, infer the smallest "
            "reasonable criterion instead of asking the user.\n\nUser request:\n"
            + str(user_message or "")[:24_000]
        )
        try:
            response = await asyncio.wait_for(
                verification_chat(provider, source="automatic_planner", session=usage_session,
                    messages=[
                        ChatMessage(
                            role="system",
                            content="You are an independent task intent and acceptance planner.",
                        ),
                        ChatMessage(role="user", content=prompt),
                    ],
                    tools=None,
                    temperature=0,
                ),
                timeout=cls._planner_timeout_seconds(),
            )
        except Exception:
            return {}
        return cls._extract_json_object(str(getattr(response, "content", "") or ""))

    @staticmethod
    def _planner_timeout_seconds() -> float:
        try:
            value = float(os.environ.get("TCODE_OUTCOME_PLANNER_TIMEOUT_SECONDS", "20"))
        except (TypeError, ValueError):
            value = 20.0
        return max(3.0, min(value, 60.0))

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        normalized = str(text or "").strip()
        if normalized.startswith("```") and normalized.endswith("```"):
            normalized = "\n".join(normalized.splitlines()[1:-1]).strip()
        try:
            value = json.loads(normalized)
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(normalized):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(normalized[index:])
            except Exception:
                continue
            if isinstance(value, dict):
                return dict(value)
        return {}

    @classmethod
    def _compile_agent_decision(
        cls,
        user_message: str,
        *,
        decision: dict[str, Any],
        minimum_level: str,
    ) -> OutcomePlan:
        proposed_level = str(decision.get("level") or minimum_level).strip().upper()
        if proposed_level not in _LEVEL_RANK:
            proposed_level = minimum_level
        level = max(
            (minimum_level, proposed_level),
            key=lambda item: _LEVEL_RANK[item],
        )
        proposed_risk = str(decision.get("risk_level") or "low").strip().lower()
        if proposed_risk not in {"low", "medium", "high", "critical"}:
            proposed_risk = "low"
        if proposed_risk in {"high", "critical"}:
            level = "V3"
        requires_tool_evidence = bool(decision.get("requires_tool_evidence", False))
        # The async completion turn consumes evidence produced outside the current
        # turn.  A turn-local tool_success criterion can never prove that earlier
        # execution and used to force otherwise successful continuations to 5%.
        # The canonical continuation prompt explicitly tells the Agent to use the
        # injected summary and not resubmit or poll the job, so semantic validation
        # is the applicable verifier for this turn.
        if cls._uses_injected_async_result(user_message):
            requires_tool_evidence = False
        if requires_tool_evidence and _LEVEL_RANK[level] < 2:
            level = "V2"
        if level == "V3" and proposed_risk == "low":
            proposed_risk = "high"

        criteria = [
            str(item).strip()
            for item in decision.get("success_criteria", [])
            if str(item or "").strip()
        ][:6]
        if not criteria:
            criteria = [
                "The final deliverable directly satisfies the user's stated goal and constraints."
            ]
        success_criteria = [
            {
                "description": item,
                "required": True,
                "verifier_refs": ["semantic"],
            }
            for item in criteria
        ]
        if requires_tool_evidence:
            success_criteria.append(
                {
                    "description": "Required tool execution completed without an error.",
                    "required": True,
                    "verifier_refs": ["tool_success"],
                    "parameters": {"require_all": True},
                }
            )
        try:
            confidence = max(0.0, min(1.0, float(decision.get("confidence", 0.75))))
        except (TypeError, ValueError):
            confidence = 0.75
        source = "planner_agent" if decision else "host_fallback"
        reason_code = str(decision.get("reason_code") or "host_complex_task_floor").strip()
        if level == "V0":
            return OutcomePlan(
                contract=OutcomeContract.compile(user_message),
                level="V0",
                source=source,
                reason_code=reason_code,
                formal_verification=False,
                confidence=confidence,
            )
        contract = OutcomeContract.compile(
            user_message,
            supplied={
                "schema_version": "tcode.outcome/v2",
                "required_proof_level": "P1",
                "risk_level": proposed_risk,
                "semantic_validation": "always",
                "max_repair_attempts": 2,
                "success_criteria": success_criteria,
                "metadata": {
                    "planning": {
                        "source": source,
                        "reason_code": reason_code,
                        "level": level,
                        "host_minimum_level": minimum_level,
                    }
                },
            },
        )
        return OutcomePlan(
            contract=contract,
            level=level,
            source=source,
            reason_code=reason_code,
            formal_verification=True,
            confidence=confidence,
        )

    @staticmethod
    def _uses_injected_async_result(user_message: str) -> bool:
        return bool(async_continuation_identifier(user_message))

    @staticmethod
    def _explicit_level(contract: OutcomeContract) -> str:
        if contract.compatibility_mode != "native_v2":
            return "V0"
        if contract.risk_level in {"high", "critical"}:
            return "V3"
        return "V2" if contract.required_proof_level in {"P2", "P3", "P4"} else "V1"

    @staticmethod
    def _contract_targets(contract: OutcomeContract) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(artifact.get("path") or "").strip()
                for criterion in contract.success_criteria
                for artifact in criterion.expected_artifacts
                if str(artifact.get("path") or "").strip()
            )
        )

    @classmethod
    def _structured_output_contract(
        cls,
        user_message: str,
        output_format: Optional[dict[str, Any]],
    ) -> Optional[OutcomePlan]:
        base = OutcomeContract.compile(user_message, output_format=output_format)
        if not base.deliverables or base.deliverables[0].kind != "json":
            return None
        schema = base.deliverables[0].schema
        verifier = "json_schema" if isinstance(schema, dict) else "json"
        proof_level = "P2" if isinstance(schema, dict) else "P1"
        parameters = {"schema": schema} if isinstance(schema, dict) else {}
        contract = OutcomeContract.compile(
            user_message,
            output_format=output_format,
            supplied={
                "schema_version": "tcode.outcome/v2",
                "required_proof_level": proof_level,
                "semantic_validation": "always",
                "max_repair_attempts": 2,
                "success_criteria": [
                    {
                        "description": (
                            "The final response is valid JSON and satisfies the supplied "
                            "schema."
                            if isinstance(schema, dict)
                            else "The final response is valid JSON without surrounding prose."
                        ),
                        "verifier_refs": [verifier],
                        "parameters": parameters,
                    }
                ],
                "metadata": {
                    "planning": {
                        "source": "automatic",
                        "reason_code": "structured_output_requirement",
                        "level": "V1",
                    }
                },
            },
        )
        return OutcomePlan(
            contract=contract,
            level="V1",
            source="automatic",
            reason_code="structured_output_requirement",
            formal_verification=True,
            confidence=1.0,
        )

    @classmethod
    def _mutation_targets(
        cls,
        user_message: str,
        workspace_root: Optional[str],
    ) -> list[str]:
        text = str(user_message or "")
        def positive_mutation(value):
            return any(not _NEGATED_MUTATION.search(value[max(0, m.start()-64):m.start()])
                       for m in _MUTATION_WORDS.finditer(value))

        if not positive_mutation(text) or not workspace_root:
            return []

        candidates: list[tuple[str, int]] = []
        # File/Path blocks are host-generated attachment metadata.  They name
        # inputs that already exist and must never become output mutation
        # targets merely because the user's request also says "生成" or
        # "create" elsewhere (for example, generate docking poses).
        occupied: list[tuple[int, int]] = [
            match.span() for pattern in (_ATTACHMENT_BLOCK, _RESOURCE_CONTEXT_BLOCK)
            for match in pattern.finditer(text)
        ]
        for pattern in (_MARKDOWN_LINK, _CODE_SPAN):
            for match in pattern.finditer(text):
                if any(start <= match.start() < end for start, end in occupied):
                    continue
                candidates.append((match.group("target"), match.start()))
                occupied.append(match.span())
        for match in _BARE_FILE.finditer(text):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            candidates.append((match.group("target"), match.start()))

        resolved: list[str] = []
        unique_candidates = list(dict.fromkeys(value for value, _ in candidates))
        for raw, position in candidates:
            # Multiple mentioned files are only accepted when the mutation intent is
            # close to that file.  A single file can use the intent from the full turn.
            nearby = text[max(0, position - 64):position]
            if re.search(r'对应(?:来源)?\s*$', nearby) and (raw.startswith(('sources/', 'source/')) or '对应来源' in nearby):
                continue
            # A read target stays an input even if an earlier clause creates
            # a Worker or another file. Never demand that sources be changed.
            inputs = list(_INPUT_WORDS.finditer(nearby))
            mutations = list(_MUTATION_WORDS.finditer(nearby))
            if inputs and (not mutations or inputs[-1].start() > mutations[-1].start()):
                continue
            if len(unique_candidates) > 1 and not positive_mutation(nearby):
                continue
            if ("/" not in raw and _WORKFLOW_WORDS.search(nearby)
                    and not positive_mutation(nearby)):
                continue  # A namespaced Workflow ID is not a promised file.
            normalized = cls._workspace_relative_file(raw, workspace_root)
            if normalized and normalized not in resolved:
                resolved.append(normalized)
        return resolved[:10]

    @staticmethod
    def _workspace_relative_file(
        raw_path: str,
        workspace_root: str,
    ) -> str:
        value = str(raw_path or "").strip().strip("<>'\"")
        value = _LINE_SUFFIX.sub("", value)
        if not value or "://" in value:
            return ""
        # Decimal values and numeric version strings are not bare file targets.
        # An explicit relative path such as ./10.6 remains a valid filename.
        if "/" not in value and "\\" not in value and re.fullmatch(r"[^A-Za-z]*\d+(?:\.\d+)+", value):
            return ""
        # Bare parameter intervals (including adjacent Chinese labels) are data,
        # not filenames. Explicit ./ paths and real extensions remain targets.
        if ("/" not in value and "\\" not in value
                and re.search(r"\d\.\.[+-]?\d", value)
                and value.rsplit(".", 1)[-1].isdigit()):
            return ""
        root = Path(workspace_root).expanduser().resolve()
        path = Path(value).expanduser()
        candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return ""
        if candidate.exists() and not candidate.is_file():
            return ""
        if not candidate.name or "." not in candidate.name:
            return ""
        return relative.as_posix()

    @staticmethod
    def _artifact_baseline(
        relative_path: str,
        workspace_root: Optional[str],
    ) -> dict[str, Any]:
        baseline: dict[str, Any] = {
            "path": relative_path,
            "existed": False,
        }
        if not workspace_root:
            return baseline
        candidate = (Path(workspace_root).expanduser().resolve() / relative_path).resolve()
        try:
            if not candidate.is_file():
                return baseline
            stat = candidate.stat()
            baseline.update(
                {
                    "existed": True,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
            if stat.st_size <= 16 * 1024 * 1024:
                digest = hashlib.sha256()
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                baseline["sha256"] = digest.hexdigest()
        except OSError:
            baseline["snapshot_error"] = True
        return baseline


__all__ = ["OutcomeContractPlanner", "OutcomePlan"]
