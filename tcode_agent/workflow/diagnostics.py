"""Workflow 通用诊断：只依据可核对的错误和校验记录归因。"""
from __future__ import annotations

from typing import Any


def workflow_diagnostic(error: str = "", report: dict | None = None, *, review_decision: str = "") -> dict[str, Any]:
    """区分配置、执行、交付和内容审核；未知错误不擅自归责用户。"""
    checks = [c for c in (report or {}).get("checks", []) if c.get("ok") is False]
    codes = {str(c.get("code") or "") for c in checks}
    text = str(error or "").lower()
    if "unknown_validator" in codes:
        category, title = "configuration", "流程配置需要调整"
        message, advice = "流程使用了当前平台不支持的校验规则。", "请修改该阶段的校验规则，再重新运行。"
    elif "missing_stage_inputs" in codes:
        category, title = "input", "本次运行的信息不足"
        message, advice = "当前阶段缺少必需输入。", "请查看缺失字段，补充输入；若字段应来自前一步，请检查阶段间的输入绑定。"
    elif review_decision.upper() in {"REVISE", "ESCALATE"} or codes.intersection({"not_enough_evidence", "unknown_evidence_refs", "missing_response_evidence_citation"}):
        category, title = "review", "结果尚未通过审核"
        message, advice = "当前结果的结论或证据未满足审核要求。", "可查看已有结果与审核意见，修订相关结论或补充证据；未通过审核的内容仅作草稿。"
    elif checks:
        category, title = "output_contract", "阶段结果未满足交付要求"
        message, advice = "这一步的返回格式、必需内容或结果文件未通过检查。", "请核对失败字段与本阶段输出要求；这可能是模型遗漏或配置不一致，目前不能据此认定内容结论错误。"
    elif any(marker in text for marker in ("timeout", "timed out", "connection", "budget exhausted", "step budget", "token", "unavailable", "interrupted")):
        category, title = "runtime", "执行过程中遇到故障"
        message, advice = "模型、工具或运行资源未能完成当前步骤。", "请检查服务连接和运行资源，恢复后重试失败阶段；此故障不代表已有结论错误。"
    else:
        category, title = "unknown", "当前步骤未完成"
        message, advice = "现有记录还不足以确认是流程配置、执行故障还是结果问题。", "请查看运行详情中的错误记录，再决定修正配置或重试；不必先删除已有结果。"
    return {"category": category, "title": title, "message": message,
            "suggestion": advice, "codes": sorted(codes), "technical_detail": str(error or "")}


def configuration_error_text(error: Exception) -> str:
    """配置入口保留技术定位，同时给出统一中文操作提示。"""
    detail = str(error)
    if detail.startswith("流程配置有误"):
        return detail
    return "流程配置有误，尚未开始执行。请检查阶段字段、依赖关系和校验规则。定位详情：" + detail
