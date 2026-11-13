"""最终回答门控检查。

基于 ToolCapability 类型系统进行轻量级校验，不引入额外 LLM 调用。
- 证据门控：检查本轮是否有"生产型"工具调用（排除辅助性工具）
- 幻觉检测：检查 final answer 是否引用了本轮未实际调用的工具名

配置来源：config/templates/symphony_route.env.example 中的环境变量，见文件内注释。
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import Message
    from ..tools.capabilities import ToolCapability

# ── 默认值（当环境变量未设置时使用） ────────────────────────────────
_DEFAULT_SUPPORT_TAGS: frozenset[str] = frozenset({"planning", "interaction", "memory", "history"})
_DEFAULT_MIN_TOOL_NAME_LEN: int = 4


def _parse_bool_env(name: str, default: bool) -> bool:
    """解析布尔型环境变量。"""
    val = str(os.environ.get(name, "") or "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


def _parse_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    """解析整型环境变量。"""
    val = str(os.environ.get(name, "") or "").strip()
    if not val:
        return default
    try:
        parsed = int(val)
    except (ValueError, TypeError):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_tags_env(name: str, default: frozenset[str]) -> frozenset[str]:
    """解析逗号分隔的标签列表环境变量。"""
    val = str(os.environ.get(name, "") or "").strip()
    if not val:
        return default
    tags = [t.strip() for t in val.split(",") if t.strip()]
    return frozenset(tags) if tags else default


def guard_enabled() -> bool:
    """总开关。

    环境变量 TCODE_FINAL_ANSWER_GUARD，默认 true。
    设为 false 可关闭全部门控。
    """
    return _parse_bool_env("TCODE_FINAL_ANSWER_GUARD", True)


def _get_support_tags() -> frozenset[str]:
    """从环境变量读取辅助性工具标签集合。

    辅助性工具标签：这些标签表示工具服务于规划、交互、记忆等辅助功能，
    而非直接执行用户任务。调用这些工具不应视为"完成了实质性工作"。

    环境变量 TCODE_EVIDENCE_GATE_SUPPORT_TAGS，逗号分隔。
    """
    return _parse_tags_env("TCODE_EVIDENCE_GATE_SUPPORT_TAGS", _DEFAULT_SUPPORT_TAGS)


def _get_min_tool_name_len() -> int:
    """从环境变量读取幻觉检测的最小工具名长度。

    只检查长度 >= 此值的工具名，避免短名误匹配（如 "read" 出现在 "already" 中）。

    环境变量 TCODE_HALLUCINATION_MIN_TOOL_NAME_LEN，默认 4。
    """
    return _parse_int_env("TCODE_HALLUCINATION_MIN_TOOL_NAME_LEN", _DEFAULT_MIN_TOOL_NAME_LEN, 2, 20)


def _collect_tool_results(
    turn_messages: list[Message],
) -> list[tuple[str, bool, str | None, dict[str, object]]]:
    """收集本轮所有工具调用结果。

    Returns:
        工具名、是否失败、调用标识和结构化元数据组成的列表。
    """
    results: list[tuple[str, bool, str | None, dict[str, object]]] = []
    for msg in turn_messages:
        if getattr(msg, "role", None) != "tool":
            continue
        for tc in getattr(msg, "tool_calls", []) or []:
            name = str(getattr(tc, "tool_name", "") or "").strip()
            if not name:
                continue
            error = bool(str(getattr(tc, "error", "") or "").strip())
            call_id = getattr(tc, "call_id", None)
            raw_metadata = getattr(tc, "metadata", None) or {}
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            results.append((name, error, call_id, metadata))

            # Delegating tools preserve the child's actual calls in a bounded
            # tool trace. Count those calls as evidence so a truthful parent
            # summary such as "browser completed" is not flagged merely
            # because the browser ran inside a dedicated subagent.
            nested_seen: set[str] = set()
            nested_names = metadata.get("tools_used")
            if isinstance(nested_names, list):
                for nested_name in nested_names[:64]:
                    normalized = str(nested_name or "").strip()
                    if not normalized or normalized in nested_seen:
                        continue
                    nested_seen.add(normalized)
                    results.append((normalized, False, str(call_id or "") or None, {}))
            tool_trace = metadata.get("tool_trace")
            if isinstance(tool_trace, list):
                for item in tool_trace[:64]:
                    if not isinstance(item, dict):
                        continue
                    normalized = str(item.get("tool_name") or "").strip()
                    if not normalized or normalized in nested_seen:
                        continue
                    nested_seen.add(normalized)
                    nested_error = bool(str(item.get("error_preview") or "").strip())
                    nested_call_id = str(item.get("call_id") or call_id or "").strip()
                    nested_metadata = item.get("metadata")
                    results.append(
                        (
                            normalized,
                            nested_error,
                            nested_call_id or None,
                            dict(nested_metadata) if isinstance(nested_metadata, dict) else {},
                        )
                    )
    return results


def _is_deterministic_terminal_failure(metadata: dict[str, object]) -> bool:
    """判断失败是否已被执行层分类为确定且无需重试的终态。

    权限拒绝、工作区保护拒绝和显式 ``retryable=false`` 都属于已经得到
    有效安全结论的工具执行结果。最终回答门控应允许 Agent 解释这种结论，
    而不应要求它重复发起同一个必然失败或越权的调用。
    """

    return bool(
        metadata.get("workspace_io_blocked")
        or metadata.get("permission_denied")
        or metadata.get("requires_final_summary")
        or metadata.get("retryable") is False
    )


def _is_support_tool(tool_name: str, capabilities: dict[str, ToolCapability]) -> bool:
    """判断工具是否为辅助性工具（规划、交互、记忆、历史查询）。

    只检查 tags 是否与配置的辅助标签有交集，不依赖 side_effect_level。
    这确保 websearch/webfetch 等只读但能获取新信息的工具不会被误判为辅助工具。
    """
    cap = capabilities.get(tool_name)
    if cap is None:
        return False
    return bool(set(cap.tags) & _get_support_tags())


def _latest_guidance_scope(
    turn_messages: list[Message],
) -> list[Message]:
    """Return the validation scope after the newest applied user Guidance.

    Guidance replaces the active plan inside one logical turn.  Tool failures
    from the superseded plan remain in history for auditability, but must not
    force the new plan into the thin-trace retry loop.  Successful earlier work
    is still available to the model and outcome ledger; this helper only narrows
    the final-answer execution gate.
    """

    for index in range(len(turn_messages) - 1, -1, -1):
        message = turn_messages[index]
        if str(getattr(message, "role", "") or "").strip().lower() != "user":
            continue
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        guidance = metadata.get("guidance")
        if isinstance(guidance, dict) and str(guidance.get("id") or "").strip():
            return turn_messages[index:]
    return turn_messages


def check_thin_trace(
    turn_messages: list[Message],
    capabilities: dict[str, ToolCapability],
) -> tuple[bool, str | None]:
    """基于 ToolCapability.tags 判断本轮是否执行了实质性工作。

    判断依据（不依赖任何硬编码的工具名列表）：
    - 工具 tags 含 planning/interaction/memory/history → 辅助工具，不计入
    - 过滤辅助工具后，如果没有任何工具调用 → 可能计划了但没有执行
    - 如果所有剩余工具都失败 → 不应假装完成

    Returns:
        (should_block, hint_message)
    """
    results = _collect_tool_results(_latest_guidance_scope(turn_messages))
    if not results:
        # 没有调用任何工具 → 可能是纯问答任务，放过
        return False, None

    productive = 0
    failed = 0
    retryable_or_unknown_failures = 0
    for tool_name, is_error, _, metadata in results:
        if _is_support_tool(tool_name, capabilities):
            continue
        productive += 1
        if is_error:
            failed += 1
            if not _is_deterministic_terminal_failure(metadata):
                retryable_or_unknown_failures += 1

    if productive == 0:
        return True, (
            "本轮你只使用了规划/交互/记忆类辅助工具，尚未执行任何实质性操作。"
            "请确认：用户任务是否真的不需要写操作、代码执行、搜索或外部交互？"
            "如果是，请明确说明原因后结束；如果否，请先完成实际工作再总结。"
        )

    if productive > 0 and failed == productive and retryable_or_unknown_failures > 0:
        return True, (
            "本轮所有操作型工具调用均失败。请分析失败原因，调整方案后重试，"
            "不要基于失败结果给出最终结论。"
        )

    return False, None


def _is_negated_tool_reference(content: str, start: int, end: int) -> bool:
    """识别“未调用某工具”这类否定陈述，避免把诚实说明误报为幻觉。"""

    before = content[max(0, start - 48) : start]
    after = content[end : min(len(content), end + 40)]
    chinese_before = re.search(
        r"(?:未|没有|尚未|并未|不曾|无法)\s*(?:实际)?\s*(?:调用|使用|用|执行|运行)"
        r"\s*(?:工具)?(?:\s*[0-9A-Za-z_.*-]+(?:\s*[/、,，或和]\s*)?)*\s*$",
        before,
    )
    english_before = re.search(
        r"(?:did\s+not|didn't|never|not)\s+(?:actually\s+)?(?:call|use|invoke|run)\s+"
        r"(?:the\s+)?(?:[0-9A-Za-z_.*-]+(?:\s*[/,]|\s+(?:or|and)\s+)?)*$",
        before,
        re.IGNORECASE,
    )
    chinese_after = re.match(r"\s*(?:未被|没有被|并未被)\s*(?:调用|使用|执行|运行)", after)
    english_after = re.match(
        r"\s+(?:was|were|is|are)\s+not\s+(?:called|used|invoked|run)",
        after,
        re.IGNORECASE,
    )
    return bool(chinese_before or english_before or chinese_after or english_after)


def _has_substantive_tool_reference(content: str, tool_id: str) -> bool:
    """按完整工具标识查找非否定引用，避免子串和否定语境误匹配。"""

    pattern = re.compile(
        rf"(?<![0-9A-Za-z_]){re.escape(tool_id)}(?![0-9A-Za-z_])",
        re.IGNORECASE,
    )
    return any(
        not _is_negated_tool_reference(content, match.start(), match.end())
        for match in pattern.finditer(content)
    )


def _without_completed_action_references(content: str, turn_messages: list[Message]) -> str:
    """Remove explicitly qualified subactions, without counting them as tools.

    A successful ``intelligent_page read`` does not establish a file ``read``
    call. Keep unqualified claims and any actions absent from the actual trace.
    """
    actions: dict[str, set[str]] = {}
    for message in turn_messages:
        if getattr(message, "role", None) != "tool":
            continue
        for call in getattr(message, "tool_calls", []) or []:
            arguments = getattr(call, "arguments", None)
            name = str(getattr(call, "tool_name", "") or "").strip()
            action = arguments.get("action") if isinstance(arguments, dict) else None
            state = getattr(call, "state", None)
            if (name and isinstance(action, str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", action)
                    and getattr(state, "value", state) == "completed"
                    and not getattr(call, "error", None)):
                actions.setdefault(name.lower(), set()).add(action.lower())
    for name, completed in actions.items():
        prefix = rf"(?<![0-9A-Za-z_]){re.escape(name)}(?![0-9A-Za-z_])"
        for action in completed:
            content = re.sub(
                prefix + rf"`?\s+(?:action\s*=\s*)?`?{re.escape(action)}`?(?![0-9A-Za-z_])",
                name, content, flags=re.IGNORECASE,
            )
        # Only a parenthesized list immediately qualified by the tool name.
        pattern = re.compile(prefix + r"`?\s*[（(]([A-Za-z_0-9\s→>/,、-]+)[）)]", re.IGNORECASE)
        def replace_chain(match: re.Match[str]) -> str:
            tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", match.group(1).lower())
            return name if tokens and all(token in completed for token in tokens) else match.group(0)
        content = pattern.sub(replace_chain, content)
        # A canvas edit action is often described in Chinese with its resource
        # rather than the tool identifier. Do not exempt a separate file edit
        # claim, an unqualified edit claim, or an action missing from this turn.
        if name == "canvas" and "edit" in completed:
            content = re.sub(
                r"(?<![0-9A-Za-z_])`?edit`?(?![0-9A-Za-z_])"
                r"(?=\s*(?:修改|更新|编辑|保存|处理)\s*(?:当前|这个|此|选中的|所选)?\s*画布)",
                "画布修改操作", content, flags=re.IGNORECASE,
            )
    return content


def check_hallucinated_tool_refs(
    final_content: str,
    turn_messages: list[Message],
    all_tool_ids: set[str],
    *,
    min_name_len: int | None = None,
) -> str | None:
    """比对 final answer 是否引用了本轮未实际调用的工具名。

    用 _CAPABILITY_MAP.keys() 作为"已知工具全集"，不硬编码任何工具名。
    只检查长度 >= min_name_len 的工具名，避免短名误匹配。

    min_name_len 默认从环境变量 TCODE_HALLUCINATION_MIN_TOOL_NAME_LEN 读取。

    Returns:
        警告文本，无问题返回 None（仅警告，不阻止 finalize）。
    """
    if min_name_len is None:
        min_name_len = _get_min_tool_name_len()
    results = _collect_tool_results(turn_messages)
    called: set[str] = {name for name, _, _, _ in results}

    checked_content = _without_completed_action_references(final_content, turn_messages)
    suspicious: list[str] = []
    for tid in all_tool_ids:
        if tid in called:
            continue
        if len(tid) < min_name_len:
            continue
        if _has_substantive_tool_reference(checked_content, tid):
            suspicious.append(tid)

    if not suspicious:
        return None

    return (
        f"⚠️ 回答中提到了以下工具但本轮并未实际调用: {', '.join(sorted(suspicious))}。"
        f"请核实这些信息是否来自上下文缓存而非凭空生成。"
    )


def check_incomplete_paper_json(
    final_content: str,
    turn_messages: list[Message],
) -> tuple[bool, str | None]:
    """Block a known bad paper-extraction terminal state.

    A response that explicitly returns the enzyme-kinetics schema, mentions
    target tables, but leaves ``kinetics_records`` empty is not a completed
    extraction. This deterministic check prevents the previous behavior of
    ending with "manual extraction required" after the local PDF was already
    available.
    """

    raw = str(final_content or "").strip()
    try:
        payload = json.loads(raw)
    except Exception:
        return False, None
    if not isinstance(payload, dict) or not isinstance(payload.get("kinetics_records"), list):
        return False, None
    if payload.get("kinetics_records"):
        return False, None

    warnings = " ".join(str(item or "") for item in payload.get("warnings", [])).lower()
    table_evidence = any(token in warnings for token in ("table 3", "table 4", "manual extraction"))
    for msg in turn_messages:
        if getattr(msg, "role", None) != "tool":
            continue
        for tc in getattr(msg, "tool_calls", []) or []:
            metadata = dict(getattr(tc, "metadata", {}) or {})
            serialized = json.dumps(metadata, ensure_ascii=False).lower()
            output = str(getattr(tc, "output", "") or "").lower()
            if '"table_count": 0' not in serialized and '"table_count":' in serialized:
                table_evidence = True
            if "table 3" in output or "table 4" in output:
                table_evidence = True
    if not table_evidence:
        return False, None
    return True, (
        "当前 JSON 声明了 Table 3/Table 4 或已有表格证据，但 kinetics_records 仍为空。"
        "这不是可接受的终态。请继续读取结构化 tables、对应 page image/OCR 和方法条件，"
        "完成逐行抽取；用户限定只基于本地 PDF 时不得改走外部搜索。"
    )
