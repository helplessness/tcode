"""统一限制完整工具回包；只分页传输，不截断科学内容或更改提交指纹。"""
import hashlib
import json


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def strip_matching_transport_fields(value, run_id, digest):
    """移除误放在科学对象里的同值传输元数据；不同值仍拒绝，不丢弃科学字段。"""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key, expected in (("run_id", run_id), ("context_digest", digest)):
        if key in result:
            if result[key] != expected:
                raise ValueError(f"nested {key} conflicts with current context")
            del result[key]
    return result


class ContextDelivery:
    """缓存绑定运行、阶段、会话与上游版本的交付页和提交回执。"""

    def __init__(self, store, snapshot, stage, owner):
        self.store = store
        self.run_id = snapshot["run"]["run_id"]
        self.path = store._run_path(self.run_id, f"context_delivery_{stage}.json")
        self.binding = hashlib.sha256(encoded({"run": snapshot["run"],
            "documents": snapshot["documents"], "stage": stage, "owner": owner}).encode()).hexdigest()

    def save(self, record):
        self.store._atomic_write(self.path, encoded(record) + "\n")

    def publish(self, context):
        # 所有附加任务、修订说明与暂存进度都先组装，再统一计算传输额度。
        record = {"binding": self.binding, "context_digest": context["context_digest"],
                  "complete": False, "committable": context.get("commit_receipt", {}).get("committable", True)}
        if len(encoded({"ok": True, "result": context})) <= 58000:
            record["complete"] = True
            self.save(record)
            return context
        segments = []
        # 先交接科学状态，再交付检索摘要／辅助元数据；不改变字段值与提交指纹。
        priority = ("citation_index", "submission_window", "saved_mechanisms",
                    "reaction", "mechanisms", "catalytic_capabilities", "cofactors", "evidence_ledger",
                    "completion_tasks", "revision_context", "staged_progress", "candidate_generation")
        ordered = [key for key in priority if key in context] + [key for key in context if key not in priority]
        for key in ordered:
            value = context[key]
            text = encoded(value)
            parts = [text[i:i + 8000] for i in range(0, len(text), 8000)]
            segments.extend({"field": key, "part": i, "parts": len(parts), "json_text": part}
                            for i, part in enumerate(parts))
        pages, page = [], []
        for segment in segments:
            if page and len(encoded([*page, segment])) > 48000:
                pages.append(page)
                page = []
            page.append(segment)
        if page:
            pages.append(page)
        record.update(pages=pages, delivered=[], token=hashlib.sha256(encoded(context).encode()).hexdigest())
        self.save(record)
        return self.page(f"ctx_{record['token']}_0")

    def page(self, cursor):
        record = self.store._read_json_if_exists(self.path) or {}
        prefix = "ctx_" + record.get("token", "") + "_"
        suffix = cursor[len(prefix):] if isinstance(cursor, str) and cursor.startswith(prefix) else ""
        if record.get("binding") != self.binding or not suffix.isdigit():
            raise ValueError("foreign or stale delivery_cursor; read current context first")
        index = int(suffix)
        pages = record.get("pages", [])
        if index >= len(pages) or any(i not in record["delivered"] for i in range(index)):
            raise ValueError("delivery pages must be read in order; use next_delivery_cursor")
        record["delivered"] = sorted(set(record["delivered"]) | {index})
        record["complete"] = len(record["delivered"]) == len(pages)
        self.save(record)
        return {"run_id": self.run_id, "context_digest": record["context_digest"],
            "context_delivery": "paged", "context_segments": pages[index],
            "next_delivery_cursor": f"{prefix}{index + 1}" if index + 1 < len(pages) else None,
            "delivery_complete": record["complete"],
            "commit_receipt": {"context_digest": record["context_digest"],
                               "committable": record["committable"]},
            "instruction": "本页是完整上下文的一部分；需要后续信息时用同一 context 工具仅传 run_id 与 delivery_cursor=next_delivery_cursor 继续，按 field 和 part 拼接 json_text。已知候选可先暂存，无须为提交读完全部页；版本、引用及科学校验仍执行。缓存分页不是新检索，不重算 embedding。"}

    def submitted_digest(self, arguments, current):
        record = self.store._read_json_if_exists(self.path) or {}
        # 取完传输页不是科学校验，也不证明模型已理解内容。
        # 提交仍绑定完整服务端上下文，不能因辅助任务尚未取完而丢弃科学草稿。
        if record.get("binding") == self.binding and not record.get("committable"):
            raise ValueError("candidate baseline is not a submission context; freeze candidate_draft first")
        if "context_digest" in arguments:
            return arguments["context_digest"]  # 显式旧值仍由原有相等校验拒绝。
        if (record.get("binding") != self.binding
                or not record.get("committable") or record.get("context_digest") != current):
            raise ValueError("no current delivered context receipt; read context before submitting")
        return record["context_digest"]
