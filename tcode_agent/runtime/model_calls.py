"""每次实际模型调用的旁路溯源；不记录提示词、响应正文、URL 或密钥。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


def new_model_call(model, **context):
    """请求模型与服务端报告模型分开；不把配置别名冒充服务端身份。"""
    requested = next((getattr(model, key, None) for key in ("_model", "model_name", "model")
                      if isinstance(getattr(model, key, None), str) and getattr(model, key)), None)
    return {"call_id": uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "requested", "provider": type(model).__name__, "requested_model": requested,
            "reported_model": None, "model_identity_source": "request_only", **context}


def finish_model_call(call, response=None, error=None):
    """缺失、超时与截断均如实记录，响应模型只能取服务端回包。"""
    metadata = getattr(response, "response_metadata", None) or {}
    reported = getattr(response, "reported_model", None) or metadata.get("model_name") or metadata.get("model")
    return {**call, "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "failed" if error is not None else "completed",
            "reported_model": reported if isinstance(reported, str) and reported else None,
            "model_identity_source": "response" if isinstance(reported, str) and reported else "request_only",
            "finish_reason": getattr(response, "finish_reason", None) or metadata.get("finish_reason"),
            **({"error_type": type(error).__name__} if error is not None else {})}


def append_model_call(directory, record):
    """追加而不覆盖续跑前的模型身份记录；该日志不进入科学推理上下文。"""
    path = Path(directory) / "model_calls.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")

