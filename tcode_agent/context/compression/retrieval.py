"""Zero-based selectors with exclusive end; no filesystem or network reads."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .reducers import decode


class Selector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["metadata", "item_range", "line_range", "json_pointer", "text_range", "query"]
    start: int = Field(default=0, ge=0)
    end: int | None = Field(default=None, ge=0)
    pointer: str = Field(default="", max_length=2048)
    query: str = Field(default="", max_length=256)


def pointer_value(data, pointer):
    if not pointer:
        return data
    if not pointer.startswith("/"):
        raise ValueError("invalid_json_pointer")
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid_json_pointer")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(data, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("invalid_array_index")
            try:
                data = data[int(token)]
            except IndexError:
                raise ValueError('json_pointer_not_found') from None
        elif isinstance(data, dict):
            try:
                data = data[token]
            except KeyError:
                raise ValueError('json_pointer_not_found') from None
        else:
            raise ValueError("invalid_json_pointer")
    return data


def select_content(text: str, selector: Selector, max_tokens: int, digest: str, *, source: dict | None = None) -> str:
    kind, data = decode(text)
    result = {"source_sha256": digest, "content_type": kind,
              "untrusted_data": True, "range_convention": "zero_based_end_exclusive"}
    if source is not None:
        result['source'] = {key: source[key] for key in ('tool', 'tool_call_id', 'message_id') if key in source}
        complete = source.get('source_complete')
        result['source_complete'] = complete if type(complete) is bool else None
        if type(source.get('retrieval_calls_remaining')) is int:
            result['retrieval_calls_remaining'] = source['retrieval_calls_remaining']
            result['retrieval_notice'] = ('回取额度已用完；不要再次回取。请用现有证据完成用户要求的交付，明确缺口。'
                if source['retrieval_calls_remaining'] == 0 else '已知关键词先用 query 定位，再精确回取；不要从头逐页扫描。')
    if selector.kind == "metadata":
        result["metadata"] = {"bytes": len(text.encode()), "lines": len(text.splitlines()),
                              "keys": list(data)[:100] if isinstance(data, dict) else [],
                              "items": len(data)}
    elif selector.kind == "json_pointer":
        if kind != "json":
            raise ValueError("json_required")
        result["value"] = pointer_value(data, selector.pointer)
    elif selector.kind == "text_range":
        value = pointer_value(data, selector.pointer) if kind == "json" else text
        if not isinstance(value, str):
            raise ValueError("string_pointer_required")
        end = min(len(value), selector.end if selector.end is not None else selector.start+1000)
        if end < selector.start or end-selector.start > 10000:
            raise ValueError("range_too_large_or_invalid")
        remaining = len(value) if selector.end is None else end
        # Binary search a Unicode-codepoint slice which fits the byte budget.
        low, high = selector.start, end
        while low < high:
            mid = (low+high+1)//2
            result.update(value=value[selector.start:mid], next_start=mid if mid < remaining else None)
            if len(json.dumps(result, ensure_ascii=False).encode()) <= max_tokens:
                low = mid
            else:
                high = mid-1
        if low == selector.start and selector.start < len(value):
            raise ValueError("selection_exceeds_budget_narrow_selector")
        result.update(value=value[selector.start:low], next_start=low if low < remaining else None)
    else:
        rows = text.splitlines() if selector.kind == "line_range" else (
            pointer_value(data, selector.pointer) if kind == "json" else data[1:] if kind == "table" else data)
        if not isinstance(rows, list):
            raise ValueError("array_pointer_required")
        if kind == 'table' and selector.kind != 'line_range':
            result['header'] = data[0]
        default_window = 1000 if selector.kind == "query" else 20
        end = min(len(rows), selector.end if selector.end is not None else selector.start + default_window)
        if end < selector.start or end-selector.start > 1000:
            raise ValueError("range_too_large_or_invalid")
        if selector.kind == "query" and not selector.query.strip():
            raise ValueError("query_required")
        result["total"] = len(rows)
        result["items"] = []
        stop = end
        for i in range(selector.start, end):
            value = rows[i]
            searchable = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if selector.kind == "query" and selector.query.casefold() not in searchable.casefold():
                continue
            result["items"].append({"index": i, "value": value})
            if len(json.dumps(result, ensure_ascii=False).encode()) + 160 > max_tokens:
                result["items"].pop()
                stop = i
                break
        remaining = len(rows) if selector.end is None else end
        result["next_start"] = stop if stop < remaining else None
        if stop == selector.start and not result["items"]:
            raise ValueError("item_exceeds_budget_use_json_pointer_or_text_range")
    encoded = json.dumps(result, ensure_ascii=False)
    # One token per UTF-8 byte is a conservative upper bound for supported BPE
    # providers. Never label chars/4 an actual tokenizer measurement.
    if len(encoded.encode()) > max_tokens:
        raise ValueError("selection_exceeds_budget_narrow_selector")
    return encoded
