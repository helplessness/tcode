"""Preflight checks before task execution."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..providers import create_provider


def _explicit_file_reference_present(task_prompt: str) -> bool:
    text = str(task_prompt or "")
    if not text.strip():
        return False
    patterns = [
        r'/[^\r\n"<>]+?\.(pdf|zip|csv|tsv|json|md|png|jpg|jpeg)\b',
        r'[A-Za-z]:\\[^\r\n"<>]+?\.(pdf|zip|csv|tsv|json|md|png|jpg|jpeg)\b',
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _looks_like_file_dependent_task(worker_name: str, task_prompt: str) -> bool:
    text = f"{worker_name}\n{task_prompt}".lower()
    keywords = [
        "pdf",
        "zip",
        "上传",
        "附件",
        "文件",
        "本地",
        "论文",
        "paper",
        "表格",
        "图片",
        "原文",
    ]
    return any(keyword in text for keyword in keywords)


def _resource_file_count(resource_context: dict[str, Any] | None) -> int:
    context = dict(resource_context or {}) if isinstance(resource_context, dict) else {}
    count = 0
    uploaded_files = context.get("uploaded_files")
    if isinstance(uploaded_files, list):
        count += sum(1 for item in uploaded_files if isinstance(item, dict) and str(item.get("path") or "").strip())
    paper_inputs = context.get("paper_inputs")
    if isinstance(paper_inputs, dict):
        count += int(paper_inputs.get("input_count", 0) or 0)
    return count


def run_preflight_checks(
    *,
    worker_name: str,
    task_prompt: str,
    workdir: str,
    resource_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    workdir_path = Path(workdir)
    checks.append(
        {
            "name": "workdir_exists",
            "status": "pass" if workdir_path.exists() else "fail",
            "detail": str(workdir_path),
        }
    )
    checks.append(
        {
            "name": "task_prompt_present",
            "status": "pass" if str(task_prompt or "").strip() else "fail",
            "detail": "任务描述不能为空",
        }
    )
    checks.append(
        {
            "name": "worker_selected",
            "status": "pass" if str(worker_name or "").strip() else "fail",
            "detail": str(worker_name or ""),
        }
    )

    provider_status = "pass"
    provider_detail = "provider ready"
    try:
        provider = create_provider(workdir=workdir)
        provider_detail = getattr(provider, "__class__", type(provider)).__name__
    except Exception as exc:
        provider_status = "fail"
        provider_detail = str(exc)
    checks.append(
        {
            "name": "provider_ready",
            "status": provider_status,
            "detail": provider_detail,
        }
    )

    file_required = _looks_like_file_dependent_task(worker_name, task_prompt)
    file_available = _explicit_file_reference_present(task_prompt) or _resource_file_count(resource_context) > 0
    checks.append(
        {
            "name": "file_resources_available",
            "status": "pass" if (not file_required or file_available) else "fail",
            "detail": (
                "检测到任务依赖本地文件/PDF，且 handoff 中已包含文件资源。"
                if (not file_required or file_available)
                else "任务看起来依赖本地文件/PDF，但任务描述与 resource_context 都没有提供可用文件路径。"
            ),
        }
    )

    has_fail = any(item.get("status") == "fail" for item in checks)
    return {
        "status": "blocked" if has_fail else "pass",
        "summary": "预检失败，任务已阻塞。" if has_fail else "预检通过，准备执行任务。",
        "next_action": "修复预检失败项后再重试。" if has_fail else "进入执行阶段。",
        "checks": checks,
    }
