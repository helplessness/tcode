"""将 TMCP 结果合同转换为可持久化的前端内容块。"""

from __future__ import annotations

import json
import math
from urllib.parse import urlparse
from typing import Any


JOB_RESULT_SCHEMA = "tmcp.job-result.v1"
JOB_SUMMARY_SCHEMA = "tcode.job-summary.v1"
TABLE_SCHEMA = "tcode.table.v1"
MAX_TABLE_ROWS = 100
MAX_COLUMNS = 64
MAX_METRICS = 64
MAX_TEXT = 16_384
_SCALAR_TYPES = (str, int, float, bool, type(None))


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def extract_tmcp_presentation(value: Any) -> dict[str, Any] | None:
    """从 collect_job_result 或包装后的 MCP 响应中提取结果合同。"""

    payload = _json_object(value)
    if payload is None:
        return None
    if payload.get("schema") == JOB_RESULT_SCHEMA:
        return payload
    direct = _json_object(payload.get("presentation"))
    if direct is not None and direct.get("schema") == JOB_RESULT_SCHEMA:
        return direct
    for key in ("structuredContent", "structured_content", "data", "result"):
        nested = extract_tmcp_presentation(payload.get(key))
        if nested is not None:
            return nested
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            nested = extract_tmcp_presentation(item.get("text"))
            if nested is not None:
                return nested
    return None


def _text(value: Any, maximum: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:maximum]


def _scalar(value: Any) -> Any:
    if not isinstance(value, _SCALAR_TYPES):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return value[:MAX_TEXT]
    return value


def _metrics(value: Any) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        key = _text(raw.get("key"), 128)
        label = _text(raw.get("label"), 256)
        metric_value = _scalar(raw.get("value"))
        if not key or not label or key in seen:
            continue
        seen.add(key)
        metric = {"key": key, "label": label, "value": metric_value}
        unit = _text(raw.get("unit"), 80)
        if unit:
            metric["unit"] = unit
        metrics.append(metric)
        if len(metrics) >= MAX_METRICS:
            break
    return metrics


def _table(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or value.get("type") != "data_table"
        or value.get("schema") != TABLE_SCHEMA
    ):
        return None
    table_id = _text(value.get("id"), 128)
    title = _text(value.get("title"), 512)
    raw_columns = value.get("columns")
    if not table_id or not title or not isinstance(raw_columns, list):
        return None
    columns: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw in raw_columns[:MAX_COLUMNS]:
        if not isinstance(raw, dict):
            continue
        key = _text(raw.get("key"), 128)
        column_title = _text(raw.get("title"), 256)
        column_type = _text(raw.get("type"), 32).lower()
        if (
            not key
            or not column_title
            or key in keys
            or column_type not in {"string", "integer", "number", "boolean"}
        ):
            continue
        keys.add(key)
        column = {"key": key, "title": column_title, "type": column_type}
        unit = _text(raw.get("unit"), 80)
        if unit:
            column["unit"] = unit
        columns.append(column)
    if not columns:
        return None
    rows: list[dict[str, Any]] = []
    raw_rows = value.get("rows") if isinstance(value.get("rows"), list) else []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        rows.append({key: _scalar(raw.get(key)) for key in keys})
        if len(rows) >= MAX_TABLE_ROWS:
            break
    primary_key = _text(value.get("primary_key"), 128)
    if primary_key not in keys:
        primary_key = columns[0]["key"]
    total_rows = value.get("total_rows")
    try:
        normalized_total = max(len(raw_rows), int(total_rows))
    except (TypeError, ValueError):
        normalized_total = len(raw_rows)
    table: dict[str, Any] = {
        "type": "data_table",
        "schema": TABLE_SCHEMA,
        "id": table_id,
        "title": title,
        "columns": columns,
        "rows": rows,
        "total_rows": normalized_total,
        "primary_key": primary_key,
        "fallback": _text(value.get("fallback"), 4000),
    }
    default_sort = value.get("default_sort")
    if isinstance(default_sort, dict):
        sort_key = _text(default_sort.get("key"), 128)
        direction = _text(default_sort.get("direction"), 8).lower()
        if sort_key in keys and direction in {"asc", "desc"}:
            table["default_sort"] = {"key": sort_key, "direction": direction}
    return table


def normalize_job_result_block(value: Any) -> dict[str, Any] | None:
    """校验已经持久化或由适配器直接生成的结果内容块。"""

    if not isinstance(value, dict):
        return None
    block_type = _text(value.get("type"), 40)
    if block_type == "data_table":
        return _table(value)
    if block_type != "job_summary" or value.get("schema") != JOB_SUMMARY_SCHEMA:
        return None
    title = _text(value.get("title"), 512)
    if not title:
        return None
    return {
        "type": "job_summary",
        "schema": JOB_SUMMARY_SCHEMA,
        "job_id": _text(value.get("job_id"), 200),
        "result_type": _text(value.get("result_type"), 160),
        "status": _text(value.get("status"), 40),
        "title": title,
        "description": _text(value.get("description"), 4000),
        "metrics": _metrics(value.get("metrics")),
        "fallback": _text(value.get("fallback"), 4000) or title,
    }


def tmcp_content_blocks(
    value: Any,
    *,
    session_id: str = "",
    server_name: str = "job-protocol-mcp",
    tool_name: str = "collect_job_result",
) -> list[dict[str, Any]]:
    """生成经过限制的 Summary 和 Table 内容块。"""

    presentation = extract_tmcp_presentation(value)
    if presentation is None:
        return []
    summary = presentation.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    title = _text(summary.get("title"), 512) or "异步任务结果"
    description = _text(summary.get("description"), 4000)
    job_id = _text(presentation.get("job_id"), 200)
    result_type = _text(presentation.get("result_type"), 160)
    status = _text(presentation.get("status"), 40)
    metrics = _metrics(summary.get("metrics"))
    blocks: list[dict[str, Any]] = [
        {
            "type": "job_summary",
            "schema": JOB_SUMMARY_SCHEMA,
            "job_id": job_id,
            "result_type": result_type,
            "status": status,
            "title": title,
            "description": description,
            "metrics": metrics,
            "fallback": "；".join(
                part for part in (title, description) if part
            )[:4000],
        }
    ]
    for raw in presentation.get("tables") if isinstance(presentation.get("tables"), list) else []:
        table = _table(raw)
        if table is not None:
            blocks.append(table)
    if session_id:
        source_payload = _json_object(value) or presentation
        for raw in presentation.get("apps") if isinstance(presentation.get("apps"), list) else []:
            if not isinstance(raw, dict) or raw.get("schema") != "tmcp.result-app.v1":
                continue
            resource_uri = _text(raw.get("resource_uri"), 2000)
            if urlparse(resource_uri).scheme != "ui":
                continue
            blocks.append(
                {
                    "type": "mcp_app",
                    "session_id": session_id,
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "resource_uri": resource_uri,
                    "tool_input": {"input": {"job_id": job_id}},
                    "tool_result": {
                        "content": [{"type": "text", "text": title}],
                        "structuredContent": source_payload,
                        "isError": status == "failed",
                    },
                    "fallback": _text(raw.get("title"), 512) or title,
                }
            )
    return blocks


def tmcp_summary_text(value: Any) -> str:
    presentation = extract_tmcp_presentation(value)
    if presentation is None:
        return ""
    summary = presentation.get("summary")
    if not isinstance(summary, dict):
        return ""
    title = _text(summary.get("title"), 512)
    description = _text(summary.get("description"), 2000)
    metrics = _metrics(summary.get("metrics"))
    metric_text = "，".join(
        f"{item['label']} {item['value']}{(' ' + item['unit']) if item.get('unit') else ''}"
        for item in metrics
        if item.get("value") is not None
    )
    return "；".join(part for part in (title, description, metric_text) if part)[:4000]
