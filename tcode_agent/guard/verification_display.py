"""Small, deterministic explanations for the user-facing verification card."""
import re


def readable_verification_text(value, fallback="现有信息还不足以确认这一点。"):
    text = str(value or "").strip()
    lowered = text.lower()
    if re.search(r"high-risk.*(?:core P2|qualifying)|required high-risk criterion", text, re.I):
        return "部分要求还缺少实际操作的记录，仅凭回答内容不能确认已经完成。"
    if "job_id" in lowered and re.search(r"本地|队列|尚无|尚未", text):
        return "任务目前只在本地排队，还没收到计算服务的接收确认，暂时不能确定是否开始运行。"
    if re.search(r"timed? ?out|timeout|超时", lowered):
        return "检查等待时间过长，暂时无法确认。"
    if re.search(r"not valid json|schema|格式", lowered):
        return "回答的格式与要求不一致。"
    if re.search(r"artifact.*(missing|not.*exist)|file.*not.*found", lowered):
        return "需要的文件或报告还没有找到。"
    if re.search(r"semantic_pending|semantic.*unavailable|验证.*不可用", lowered):
        return "这部分检查尚未完成，暂时无法判断。"
    if re.search(r"source_coverage_missing|缺少.*来源", lowered):
        return "还没有足够的来源支持这部分结论。"
    if re.search(r"validator.*(invalid|empty)|缺少明确的逐项", lowered):
        return "这项要求还没有可用的核对说明，暂时无法判断。"
    if re.search(r"read.?back|读回|重新.*核对", lowered):
        return "报告保存后还没有重新打开核对，内容暂未确认。"
    if re.search(r"tool.*(missing|required|evidence)|tool_evidence", lowered):
        return "还没有看到所需操作的结果，无法确认是否完成。"
    return _plain_text(text, fallback)


def _plain_text(text, fallback):
    text = str(text or "").strip()
    text = re.sub(r"\bLOCAL-\d+\b", "", text)
    text = text.replace("job_id", "任务编号")
    text = re.sub(r"(?:criterion|verification|outcome|workflow|call|turn|session|contract|bundle|wf)[_-][A-Za-z0-9_-]+", "", text, flags=re.I)
    text = re.sub(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_-]+\b", "相关项目", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[0-9a-f]{24,}\b", "", text, flags=re.I)
    text = re.sub(r"(?:/[^\s，。；]+){2,}|[{}\[\]`<>]", "", text).strip(" :：,，;；-\n")
    # Do not surface untranslated diagnostics, internal keys or identifiers.
    if not re.search(r"[\u4e00-\u9fff]{2}", text):
        return fallback
    return text[:160]


def verification_findings(payload):
    findings = []

    def add(status, title, reason):
        item = {"status": status, "title": _plain_text(title, "回答中的一项要求"),
                "reason": readable_verification_text(reason)}
        if item["reason"] == "现有信息还不足以确认这一点。":
            item["reason"] = "这份检查记录没有保存具体原因，不能据此认定结果有误。"
        if str(reason or "").strip() == "semantic_pending":
            item["reason"] = "还没有完成对这项要求的独立核对，不代表回答一定有误。"
            if re.search(r"scores\.json|评分.*行数", str(title)):
                item.update(title="评分结果和来源说明", reason="还没有核对评分文件的行数、链和位点是否对应，以及工具版本是否有来源记录。")
            elif re.search(r"保留旧记录|不恢复.*不重提", str(title)):
                item.update(title="旧任务是否保持不变", reason="还没有核对旧任务是否保持原样，以及是否避免了恢复或重复提交。")
        if not any(old["status"] == status and old["reason"] == item["reason"]
                   and (old["title"] == item["title"] or item["title"] in {"未满足的要求", "暂未核实的内容", "需要核对的内容"})
                   for old in findings):
            findings.append(item)

    for item in payload.get("findings") or []:
        if isinstance(item, dict) and item.get("status") in {"failed", "unverified"}:
            add(item["status"], item.get("title"), item.get("reason"))
    for item in payload.get("criterion_results") or []:
        if not isinstance(item, dict) or item.get("status") == "pass":
            continue
        reason = "；".join(str(x) for x in item.get("observations", []) if x)
        add("failed" if item.get("status") == "fail" else "unverified",
            item.get("description"), reason or item.get("reason_code"))
    for key, title, status in (("failed_criteria", "未满足的要求", "failed"),
                               ("unsupported_claims", "结论依据不足", "unverified"),
                               ("missing_deliverables", "缺少交付内容", "failed"),
                               ("warnings", "暂未核实的内容", "unverified")):
        for reason in payload.get(key) or []:
            if re.fullmatch(r"criterion_[\w-]+", str(reason)):
                continue  # The corresponding named requirement is shown above.
            uncertain = payload.get("validator") == "semantic_unavailable" or (
                key == "failed_criteria" and payload.get("verdict") in {"partial", "blocked"})
            add("unverified" if uncertain else status, title, reason)
    if not findings:
        for reason in payload.get("issues") or []:
            add("unverified", "需要核对的内容", reason)
    return findings[:10]


def delivery_message_verification(message, metadata):
    """Recover explanations for an old saved delivery from its own raw check only."""
    saved = dict(getattr(message, "verification", None) or {})
    turn = dict((metadata or {}).get("last_turn") or {})
    raw = dict(dict(turn.get("extra") or {}).get("outcome_validation") or {})
    if saved.get("advisory") and turn.get("final_message_id") == message.id and raw:
        recovered = verification_findings(raw)
        if recovered:
            saved["findings"] = recovered
    return saved
