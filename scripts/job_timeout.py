"""Stdlib-only timeout contract for the two Symphony adapter scripts."""
import json
from decimal import Decimal, InvalidOperation


def job_wait_timeout_seconds(value=0):
    """Match Job Protocol's accepted values and effective finite bounds."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("timeout_seconds must be a non-negative integer")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("timeout_seconds must be a non-negative integer") from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("timeout_seconds must be a non-negative integer")
    return 0 if number.is_zero() else max(60, int(min(number, 86400)))


def local_wait_timeout_error():
    return json.dumps({
        "isError": True,
        "failure_kind": "local_wait_timeout",
        "remote_outcome": "unknown",
        "error": "本地等待已超时，远端计算是否结束及结果尚未确认；请先核查原任务，不要直接重提。",
    }, ensure_ascii=False)
