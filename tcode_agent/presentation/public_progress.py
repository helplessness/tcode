"""Small public-result projections shared by ordinary tasks and workflows."""

import json
import re
from typing import Any


_TECHNICAL_DETAIL_REQUEST = re.compile(
    r"(?:技术|实现|后端|数据库|SQLite|MCP|RDKit|Workspace|工具调用|运行细节).{0,24}"
    r"(?:说明|解释|列出|详情|诊断|核实|为什么|为何|是否|怎么)|"
    r"(?:说明|解释|列出|诊断|核实).{0,24}(?:技术|实现|后端|数据库|SQLite|MCP|RDKit|Workspace|工具调用|运行细节)",
    re.I,
)


def technical_details_requested(request: Any) -> bool:
    """True only when the user asks to inspect implementation/runtime details."""
    return isinstance(request, str) and bool(_TECHNICAL_DETAIL_REQUEST.search(request))


def scientific_user_text(value: Any, *, request: str = "") -> str:
    """Translate implementation boundaries into their scientific meaning."""
    if not isinstance(value, str) or technical_details_requested(request):
        return value if isinstance(value, str) else ""
    text = value
    replacements = (
        (
            r"在仅使用本体插件与其原文证据、(?:仅依据当前可核验证据|无联网)的范围内",
            "根据当前可核验的反应与酶证据",
        ),
        (r"仅使用本体插件与其原文证据、?", "依据当前可核验的反应与酶证据"),
        (r"(?:当前)?仅(?:有)?五张(?:本地|冻结)?(?:知识)?卡(?:片)?", "当前可核验证据覆盖有限"),
        (r"(?:离线、?)?无联网(?:/外部\s*MCP)?(?:/文档解析)?", "仅依据当前可核验证据"),
        (r"离线、?无文档解析", "仅依据当前可核验证据"),
        (r"无外部\s*MCP(?:/文档解析)?", "仅依据当前可核验证据"),
        (r"无文档解析", "仅依据当前可核验证据"),
        (r"无(?:离线)?序列/结构候选库", "尚缺可用于扩展候选的序列与结构证据"),
        (r"无\s*RDKit/原子映射", "尚未验证具体原子对应与反应配平"),
        (r"无原子映射", "尚未验证具体原子对应与反应配平"),
        (r"未执行\s*RDKit、?原子映射、?质量守恒或结构级验证", "尚未验证具体原子对应、反应配平与结构层面的可行性"),
        (r"未执行原子映射、?质量守恒或结构级验证", "尚未验证具体原子对应、反应配平与结构层面的可行性"),
        (r"私有\s*Workspace\s*本体不可用(?:（available=false）)?", "本次结论仅基于当前可核验的证据"),
        (r"Workspace\s*私有知识不可用(?:（available=false）)?", "本次结论仅基于当前可核验的证据"),
        (r"丙烯酸与\s*ThDP\s*中间体无实体", "当前证据未覆盖丙烯酸与 ThDP 中间体"),
        (r"本地无(?:任何)?([^；。\n]{1,30})实体", r"当前可核验证据未覆盖\1"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    text = re.sub(r"(?m)^-\s*Run ID：.*\n?", "", text, flags=re.I)
    text = re.sub(
        r"(?m)^-\s*知识边界：.*$",
        "- 证据范围：当前已核验的公开及获准使用证据",
        text,
    )
    text = re.sub(
        r"(?m)^-\s*全程离线：.*$",
        "- 结论仅依据当前已核验的证据；未覆盖的来源可能改变候选排序与结论。",
        text,
    )
    text = re.sub(
        r"(?m)^-\s*仅依据当前可核验证据能力，未对文献全文做结构化抽取，仅基于卡内逐字引文片段。$",
        "- 当前证据主要来自已整理的原文引文片段，尚缺更广泛的原文证据。",
        text,
    )
    text = re.sub(
        r"(?m)^-\s*本次结论仅基于当前可核验的证据，.*$",
        "- 本次结论仅基于当前可核验的公开证据。",
        text,
    )
    text = re.sub(
        r"未做原子映射或质量守恒校验",
        "尚未验证具体原子对应与反应配平",
        text,
    )
    text = re.sub(r"均无实体\s*ID", "均缺少可核验记录", text, flags=re.I)
    text = re.sub(r"补充并冻结(.{1,100}?)的知识卡", r"补充并核验\1的证据记录", text)
    text = re.sub(r"(?:；\s*){2,}", "；", text)
    text = re.sub(r"仅依据当前可核验证据(?:；\s*仅依据当前可核验证据)+", "仅依据当前可核验证据", text)
    return text


def public_text(value: Any, *, limit: int = 260) -> str:
    if not isinstance(value, str):
        return ""
    if value.lstrip().startswith(("{", "[")) or re.search(r"```|<[^>]+>|private_prompt|system.prompt|reasoning_content|chain.of.thought|api[_-]?key|password|authorization\s*:|sk-[A-Za-z0-9]{16,}|忽略.*指令|ignore.*instructions|系统提示|隐藏推理", value, re.I):
        return ""
    value = re.sub(r"(?:assertion|ev):[\w:./-]+|ERD-CARD-\d+", "来源证据", value, flags=re.I)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def tool_public_progress(call: Any) -> list[dict[str, Any]]:
    """Project explicit public tool facts, never agent prose or hidden reasoning.

    Unknown tool schemas stay silent unless they opt into public_progress.
    Evidence IDs remain sidecar provenance, not user-facing jargon.
    """
    state = getattr(getattr(call, "state", None), "value", "")
    metadata = getattr(call, "metadata", {}) or {}
    name = str(getattr(call, "tool_name", "") or "")
    arguments = getattr(call, "arguments", {}) or {}
    if name == "glob" and state == "completed" and not getattr(call, "error", None) and isinstance(metadata, dict):
        pattern = public_text(arguments.get("pattern"), limit=120)
        if (metadata.get("count") == 0 and not str(getattr(call, "output", "") or "").strip()
                and pattern and re.fullmatch(r"[\w.-]+", pattern) and pattern not in {".", ".."}):
            return [{"topic": "missing-file:" + pattern.casefold(), "text": f"当前检索范围内没有找到 {pattern}，因此还没有可读取的内容；这个结果只说明该范围内未匹配到文件。", "source_refs": [str(call.call_id)], "epistemic_status": "unknown"}]
    if state != "completed" or not isinstance(metadata, dict) or metadata.get("ok") is not True:
        return []
    public = metadata.get("public_progress")
    if isinstance(public, dict):
        kind = public.get("epistemic_status")
        refs = public.get("source_refs")
        finding = public_text(public.get("finding"), limit=240)
        if kind not in {"reported", "hypothesis", "unknown"} or not finding or not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and 0 < len(ref) < 200 and public_text(ref, limit=200) for ref in refs):
            return []
        parts = [finding, public_text(public.get("meaning"), limit=120), public_text(public.get("next_action"), limit=120)]
        text = " ".join(part for part in parts if part)
        if kind == "hypothesis":
            text = "暂定判断（尚未验证）：" + text
        elif kind == "unknown":
            text = "尚未确认：" + text
        return [{"topic": "public:" + finding, "text": text, "source_refs": refs[:8], "epistemic_status": kind}]

    if name not in {"enzyme_entity_resolve", "enzyme_entity_read_evidence", "enzyme_mechanism_context", "enzyme_screen_context"}:
        return []
    output = str(getattr(call, "output", "") or "")
    if len(output) > 100000:
        return []
    try:
        envelope = json.loads(output)
    except (ValueError, TypeError):
        return []
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return []
    result = envelope.get("result")
    if not isinstance(result, dict):
        return []
    if name in {"enzyme_mechanism_context", "enzyme_screen_context"}:
        cards, selected = result.get("cards"), result.get("selected_refs")
        if not isinstance(cards, list) or not isinstance(selected, list):
            return []
        updates = []
        for card in cards[:8]:
            if not isinstance(card, dict) or not isinstance(card.get("claims"), list):
                continue
            label = public_text(card.get("display_name"), limit=100)
            for claim in card["claims"][:8]:
                if not isinstance(claim, dict) or claim.get("role") != "evidence" or claim.get("ref") not in selected:
                    continue
                statement = public_text(claim.get("statement"), limit=220)
                if not label or not statement or statement.startswith(("{", "[")):
                    continue
                statement = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", statement).replace("#", "")
                updates.append({"topic": "read-card:" + str(card.get("card_id") or label), "text": f"已读到 {label} 的证据片段：{statement}。这些资料的适用范围仍须保留，不能直接升级为本次目标活性证据。", "source_refs": [claim["ref"]], "epistemic_status": "reported"})
                break
        return updates[:2]
    if envelope.get("operation") != name:
        return []
    if name == "enzyme_entity_resolve":
        query = public_text(result.get("normalized_query"), limit=100)
        if result.get("status") != "no_match" or result.get("results") != [] or not query:
            return []
        return [{"topic": "coverage:" + query, "text": f"当前证据范围未找到“{query}”的实体记录。这是知识覆盖缺口，不能据此认定它或相关反应不存在。", "source_refs": [str(call.call_id)], "epistemic_status": "unknown"}]

    evidence = result.get("evidence")
    assertions = result.get("assertions")
    if result.get("citation_ready") is not True or not isinstance(evidence, list) or not isinstance(assertions, list):
        return []
    read_refs = {item.get("evidence_id") for item in evidence if isinstance(item, dict) and isinstance(item.get("evidence_id"), str) and public_text(item.get("quote"), limit=600)}
    updates = []
    for item in assertions[:24]:
        if not isinstance(item, dict) or item.get("scope") != "public" or item.get("epistemic_kind") != "card_reported" or item.get("polarity") != "supports" or item.get("conflict_state") not in {None, "none"}:
            continue
        subject, obj = item.get("subject"), item.get("object")
        refs = item.get("evidence_ids")
        if not isinstance(subject, dict) or not isinstance(obj, dict) or not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref in read_refs for ref in refs):
            continue
        subject_label = public_text(subject.get("label"), limit=100)
        object_label = public_text(obj.get("label"), limit=220)
        if not subject_label or not object_label or item.get("predicate") != "catalyzes_reported":
            continue
        object_label = re.sub(r"^RHEA:\d+\s*[｜|]\s*", "", object_label)
        text = f"已读到 {subject_label} 的已报道反应：{object_label}。这些是它的已报道能力，不是本次目标活性的证明；仍需核对它是否覆盖目标所需的全部变化。"
        updates.append({"topic": "reported-reaction:" + str(subject.get("entity_id") or subject_label), "text": text, "source_refs": refs, "epistemic_status": "reported"})
    return updates[:2]
