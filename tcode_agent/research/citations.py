"""Repair a missing delimiter only for citations issued by a successful reader.

This neither resolves unknown IDs nor changes source status or claim support.
"""
import json
import re

_CITATION = re.compile(r'\[knowledge:([A-Za-z0-9_-]{1,2400})\]')
_OPEN = re.compile(r'\[knowledge:([A-Za-z0-9_-]{1,2400})(?=$|[\s。，,.;；:：!?！？)*])')
_CODE = re.compile(r'(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)')


def repair_knowledge_delimiters(text, messages):
    if '[knowledge:' not in text:
        return text, 0
    issued = set()
    for message in messages:
        if message.role != 'tool':
            continue
        for call in message.tool_calls or []:
            if (call.tool_name != 'knowledge_search' or call.error
                    or call.arguments.get('action', 'search') != 'read'):
                continue
            try:
                data = json.loads(call.output)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict) or data.get('核对情况') != '已读取当前保存来源':
                continue
            for source in [data, *(data.get('关联依据') or [])]:
                if isinstance(source, dict):
                    match = _CITATION.fullmatch(str(source.get('引用') or ''))
                    if match:
                        issued.add(match.group(1))
    repairs = 0

    def replace(match):
        nonlocal repairs
        if match.group(1) not in issued:
            return match.group(0)
        repairs += 1
        return match.group(0) + ']'

    chunks = _CODE.split(text)
    return ''.join(chunk if index % 2 else _OPEN.sub(replace, chunk)
                   for index, chunk in enumerate(chunks)), repairs
