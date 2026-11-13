from __future__ import annotations

import calendar
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LOCAL_TIME = "09:00"
SUPPORTED_SCHEDULE_TYPES = {
    "at",
    "delay",
    "daily",
    "weekly",
    "monthly",
    "weekdays",
    "cron",
}


class ScheduleValidationError(ValueError):
    pass


def _zone(value: Any) -> ZoneInfo:
    name = str(value or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleValidationError(f"无效时区：{name}") from exc


def _parse_clock(value: Any, *, default: str = DEFAULT_LOCAL_TIME) -> tuple[int, int, int]:
    text = str(value or default).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if not match:
        raise ScheduleValidationError("时间必须使用 HH:MM 或 HH:MM:SS 格式")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise ScheduleValidationError("时间超出有效范围")
    return hour, minute, second


def _parse_datetime(value: Any, tz: ZoneInfo) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ScheduleValidationError("指定日期任务缺少执行日期")
    date_only = bool(re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text))
    if date_only:
        text = f"{text}T{DEFAULT_LOCAL_TIME}:00"
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ScheduleValidationError("执行日期必须是 ISO 8601 格式") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _as_utc_timestamp(value: datetime) -> float:
    return value.astimezone(timezone.utc).timestamp()


def _local_datetime(timestamp: float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(float(timestamp), timezone.utc).astimezone(tz)


def _next_daily(after: datetime, clock: tuple[int, int, int]) -> datetime:
    candidate = after.replace(
        hour=clock[0], minute=clock[1], second=clock[2], microsecond=0
    )
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _next_weekly(
    after: datetime,
    clock: tuple[int, int, int],
    weekdays: Iterable[int],
) -> datetime:
    allowed = {int(item) for item in weekdays}
    if not allowed or any(item < 0 or item > 6 for item in allowed):
        raise ScheduleValidationError("每周任务必须指定 0（周一）到 6（周日）的星期值")
    for offset in range(0, 15):
        current = after.date() + timedelta(days=offset)
        if current.weekday() not in allowed:
            continue
        candidate = datetime.combine(current, datetime.min.time(), tzinfo=after.tzinfo).replace(
            hour=clock[0], minute=clock[1], second=clock[2]
        )
        if candidate > after:
            return candidate
    raise ScheduleValidationError("无法计算下一次每周执行时间")


def _next_monthly(after: datetime, clock: tuple[int, int, int], day: int) -> datetime:
    if day < 1 or day > 31:
        raise ScheduleValidationError("每月执行日期必须在 1 到 31 之间")
    year, month = after.year, after.month
    for _ in range(0, 36):
        last_day = calendar.monthrange(year, month)[1]
        if day <= last_day:
            candidate = datetime(
                year,
                month,
                day,
                clock[0],
                clock[1],
                clock[2],
                tzinfo=after.tzinfo,
            )
            if candidate > after:
                return candidate
        month += 1
        if month > 12:
            month = 1
            year += 1
    raise ScheduleValidationError("无法计算下一次每月执行时间")


class CronExpression:
    """Small Vixie-style 5/6-field cron parser with timezone support."""

    _RANGES = ((0, 59), (0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def __init__(self, expression: str):
        self.expression = str(expression or "").strip()
        fields = self.expression.split()
        if len(fields) not in {5, 6}:
            raise ScheduleValidationError("Cron 表达式必须包含 5 或 6 个字段")
        self.has_seconds = len(fields) == 6
        if not self.has_seconds:
            fields = ["0", *fields]
        self.raw_fields = tuple(fields)
        self.values = tuple(
            self._parse_field(raw, low, high, dow=index == 5)
            for index, (raw, (low, high)) in enumerate(zip(fields, self._RANGES))
        )
        self.dom_wildcard = fields[3].startswith("*")
        self.dow_wildcard = fields[5].startswith("*")

    @classmethod
    def _parse_field(cls, raw: str, low: int, high: int, *, dow: bool) -> set[int]:
        values: set[int] = set()
        for part in str(raw or "").split(","):
            part = part.strip()
            if not part:
                raise ScheduleValidationError("Cron 字段不能为空")
            base, slash, step_text = part.partition("/")
            try:
                step = int(step_text) if slash else 1
            except ValueError as exc:
                raise ScheduleValidationError(f"无效 Cron 步长：{part}") from exc
            if step <= 0:
                raise ScheduleValidationError("Cron 步长必须大于 0")
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start_text, end_text = base.split("-", 1)
                try:
                    start, end = int(start_text), int(end_text)
                except ValueError as exc:
                    raise ScheduleValidationError(f"无效 Cron 范围：{part}") from exc
            else:
                try:
                    start = int(base)
                except ValueError as exc:
                    raise ScheduleValidationError(f"无效 Cron 值：{part}") from exc
                end = high if slash else start
            if start < low or end > high or start > end:
                raise ScheduleValidationError(f"Cron 值超出范围：{part}")
            values.update(range(start, end + 1, step))
        if dow and 7 in values:
            values.add(0)
            values.discard(7)
        return values

    def _date_matches(self, candidate: datetime) -> bool:
        dom_match = candidate.day in self.values[3]
        cron_dow = (candidate.weekday() + 1) % 7
        dow_match = cron_dow in self.values[5]
        if self.dom_wildcard and self.dow_wildcard:
            return True
        if self.dom_wildcard:
            return dow_match
        if self.dow_wildcard:
            return dom_match
        return dom_match or dow_match

    def _minute_matches(self, candidate: datetime) -> bool:
        return (
            candidate.minute in self.values[1]
            and candidate.hour in self.values[2]
            and candidate.month in self.values[4]
            and self._date_matches(candidate)
        )

    def next_after(self, after: datetime) -> datetime:
        local_after = after
        minute = local_after.replace(second=0, microsecond=0)
        seconds = sorted(self.values[0])
        max_minutes = 60 * 24 * 366 * 6
        for offset in range(max_minutes):
            candidate_minute = minute + timedelta(minutes=offset)
            if not self._minute_matches(candidate_minute):
                continue
            for second in seconds:
                candidate = candidate_minute.replace(second=second)
                if candidate > local_after:
                    return candidate
        raise ScheduleValidationError("Cron 表达式在未来六年内没有可执行时间")


def compute_next_run(
    schedule_type: str,
    schedule: dict[str, Any],
    timezone_name: str,
    *,
    after_ts: float | None = None,
) -> float | None:
    kind = str(schedule_type or "").strip().lower()
    if kind not in SUPPORTED_SCHEDULE_TYPES:
        raise ScheduleValidationError(f"不支持的任务类型：{kind}")
    tz = _zone(timezone_name)
    after_value = float(time.time() if after_ts is None else after_ts)
    after = _local_datetime(after_value, tz)
    data = dict(schedule or {})

    if kind in {"at", "delay"}:
        run_at = data.get("run_at")
        if isinstance(run_at, (int, float)):
            return float(run_at)
        return _as_utc_timestamp(_parse_datetime(run_at, tz))
    if kind == "daily":
        candidate = _next_daily(after, _parse_clock(data.get("time")))
    elif kind == "weekdays":
        candidate = _next_weekly(after, _parse_clock(data.get("time")), range(0, 5))
    elif kind == "weekly":
        candidate = _next_weekly(
            after,
            _parse_clock(data.get("time")),
            data.get("weekdays") or [],
        )
    elif kind == "monthly":
        candidate = _next_monthly(
            after,
            _parse_clock(data.get("time")),
            int(data.get("day") or 0),
        )
    else:
        expression = CronExpression(str(data.get("expression") or ""))
        candidate = expression.next_after(after)
    return _as_utc_timestamp(candidate)


def normalize_task_spec(
    payload: dict[str, Any],
    *,
    now_ts: float | None = None,
    require_future: bool = True,
) -> dict[str, Any]:
    now_value = float(time.time() if now_ts is None else now_ts)
    instruction = str(payload.get("instruction") or "").strip()
    if not instruction:
        raise ScheduleValidationError("任务指令不能为空")
    if len(instruction) > 20_000:
        raise ScheduleValidationError("任务指令不能超过 20000 个字符")
    name = str(payload.get("name") or instruction[:60] or "定时任务").strip()[:120]
    timezone_name = str(payload.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    tz = _zone(timezone_name)
    schedule_type = str(payload.get("schedule_type") or "").strip().lower()
    if schedule_type not in SUPPORTED_SCHEDULE_TYPES:
        raise ScheduleValidationError("请选择有效的任务类型")
    schedule = dict(payload.get("schedule") or {})

    if schedule_type == "delay":
        try:
            delay_seconds = float(schedule.get("delay_seconds") or 0)
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("延迟秒数必须是数字") from exc
        if delay_seconds <= 0:
            raise ScheduleValidationError("延迟时间必须大于 0")
        schedule = {
            "delay_seconds": delay_seconds,
            "run_at": now_value + delay_seconds,
        }
    elif schedule_type == "at":
        raw_run_at = schedule.get("run_at")
        if isinstance(raw_run_at, (int, float)):
            schedule = {"run_at": float(raw_run_at)}
        else:
            parsed = _parse_datetime(raw_run_at, tz)
            schedule = {"run_at": _as_utc_timestamp(parsed)}
    elif schedule_type in {"daily", "weekdays"}:
        clock = _parse_clock(schedule.get("time"))
        schedule = {"time": f"{clock[0]:02d}:{clock[1]:02d}:{clock[2]:02d}"}
    elif schedule_type == "weekly":
        raw_days = schedule.get("weekdays") or []
        try:
            weekdays = sorted({int(item) for item in raw_days})
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("星期值必须是 0 到 6 的整数") from exc
        if not weekdays or any(item < 0 or item > 6 for item in weekdays):
            raise ScheduleValidationError("每周任务至少选择一个星期")
        clock = _parse_clock(schedule.get("time"))
        schedule = {
            "weekdays": weekdays,
            "time": f"{clock[0]:02d}:{clock[1]:02d}:{clock[2]:02d}",
        }
    elif schedule_type == "monthly":
        try:
            day = int(schedule.get("day") or 0)
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("每月日期必须是整数") from exc
        if day < 1 or day > 31:
            raise ScheduleValidationError("每月日期必须在 1 到 31 之间")
        clock = _parse_clock(schedule.get("time"))
        schedule = {
            "day": day,
            "time": f"{clock[0]:02d}:{clock[1]:02d}:{clock[2]:02d}",
        }
    else:
        expression = str(schedule.get("expression") or "").strip()
        CronExpression(expression)
        schedule = {"expression": expression}

    next_run_at = compute_next_run(
        schedule_type,
        schedule,
        timezone_name,
        after_ts=now_value,
    )
    if require_future and (next_run_at is None or next_run_at <= now_value):
        raise ScheduleValidationError("首次执行时间必须晚于当前时间")

    normalized = {
        "name": name,
        "instruction": instruction,
        "schedule_type": schedule_type,
        "schedule": schedule,
        "timezone": timezone_name,
        "next_run_at": next_run_at,
        "source_session_id": str(payload.get("source_session_id") or "").strip()[:160],
        "agent_name": str(payload.get("agent_name") or "build").strip()[:120] or "build",
        "workdir": str(payload.get("workdir") or "").strip(),
        "created_via": str(payload.get("created_via") or "api").strip()[:40] or "api",
    }
    return normalized


_WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


def _extract_chinese_clock(text: str) -> tuple[str | None, str]:
    match = re.search(
        r"(?:(上午|早上|下午|晚上|中午)\s*)?(\d{1,2})(?:[:：](\d{1,2})|点(?:(\d{1,2})分|(半))?|时(?:(\d{1,2})分)?)",
        text,
    )
    if not match:
        colon = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
        if not colon:
            return None, text
        hour, minute = int(colon.group(1)), int(colon.group(2))
        if hour > 23 or minute > 59:
            return None, text
        return f"{hour:02d}:{minute:02d}:00", text[: colon.start()] + text[colon.end() :]
    period = match.group(1) or ""
    hour = int(match.group(2))
    minute = 30 if match.group(5) else int(match.group(3) or match.group(4) or match.group(6) or 0)
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if period in {"上午", "早上"} and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None, text
    return f"{hour:02d}:{minute:02d}:00", text[: match.start()] + text[match.end() :]


def _clean_instruction(text: str) -> str:
    cleaned = re.sub(r"^[，,。\s]*(请|麻烦)?(到时|届时)?", "", text).strip(" ，,。")
    cleaned = re.sub(r"^提醒我", "提醒用户：", cleaned)
    return cleaned.strip(" ，,。")


def _number_text(value: str) -> float:
    text = str(value or "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return float(digits[text])
    if text == "十":
        return 10.0
    if "十" in text:
        left, right = text.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return float(tens * 10 + ones)
    raise ValueError(f"无法识别数字：{text}")


def parse_natural_language(
    text: str,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Parse common Chinese scheduling phrases; unresolved semantics stay explicit."""

    raw = str(text or "").strip()
    if not raw:
        return {"requires_clarification": True, "questions": ["你希望在什么时间执行什么任务？"]}
    tz = _zone(timezone_name)
    now_value = float(time.time() if now_ts is None else now_ts)
    now = _local_datetime(now_value, tz)
    questions: list[str] = []
    clock, without_clock = _extract_chinese_clock(raw)
    clock = clock or f"{DEFAULT_LOCAL_TIME}:00"
    schedule_type = ""
    schedule: dict[str, Any] = {}
    remaining = without_clock

    cron_match = re.search(r"(?:cron\s*[:：]?\s*)([^，,；;]+)", raw, re.IGNORECASE)
    if cron_match:
        expression = cron_match.group(1).strip().strip("`\"")
        try:
            CronExpression(expression)
            schedule_type, schedule = "cron", {"expression": expression}
        except ScheduleValidationError as exc:
            questions.append(str(exc))
        remaining = raw[: cron_match.start()] + raw[cron_match.end() :]

    if not schedule_type:
        delay_match = re.search(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*(秒|分钟|分|小时|天)\s*(?:以?后|之后)", raw)
        if delay_match:
            amount = _number_text(delay_match.group(1))
            unit = delay_match.group(2)
            multiplier = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "天": 86400}[unit]
            schedule_type = "delay"
            schedule = {"delay_seconds": amount * multiplier}
            remaining = raw[: delay_match.start()] + raw[delay_match.end() :]

    if not schedule_type and re.search(r"每个?工作日|工作日每天", raw):
        schedule_type, schedule = "weekdays", {"time": clock}
        remaining = re.sub(r"每个?工作日|工作日每天", "", without_clock)

    if not schedule_type and re.search(r"每天|每日", raw):
        schedule_type, schedule = "daily", {"time": clock}
        remaining = re.sub(r"每天|每日", "", without_clock)

    if not schedule_type:
        weekly = re.search(r"每周([一二三四五六日天](?:[、,，和及至到一二三四五六日天]*))?", raw)
        if weekly:
            day_text = weekly.group(1) or ""
            days = sorted({_WEEKDAY_MAP[ch] for ch in day_text if ch in _WEEKDAY_MAP})
            if not days:
                questions.append("每周任务具体在星期几执行？")
            else:
                schedule_type, schedule = "weekly", {"weekdays": days, "time": clock}
            remaining = re.sub(r"每周[一二三四五六日天、,，和及至到]*", "", without_clock, count=1)

    if not schedule_type:
        monthly = re.search(r"每月(?:的)?(\d{1,2})(?:号|日)", raw)
        if monthly:
            schedule_type, schedule = "monthly", {
                "day": int(monthly.group(1)),
                "time": clock,
            }
            remaining = re.sub(r"每月(?:的)?\d{1,2}(?:号|日)", "", without_clock, count=1)

    target_date: date | None = None
    date_span: tuple[int, int] | None = None
    if not schedule_type:
        next_week = re.search(r"下周([一二三四五六日天])", raw)
        if next_week:
            monday = now.date() - timedelta(days=now.weekday()) + timedelta(days=7)
            target_date = monday + timedelta(days=_WEEKDAY_MAP[next_week.group(1)])
            date_span = next_week.span()
        elif "下周" in raw:
            questions.append("“下周”具体是星期几？")

    if not schedule_type and target_date is None:
        relative_dates = (("后天", 2), ("明天", 1), ("今天", 0))
        for word, offset in relative_dates:
            index = raw.find(word)
            if index >= 0:
                target_date = now.date() + timedelta(days=offset)
                date_span = (index, index + len(word))
                break

    if not schedule_type and target_date is None:
        explicit = re.search(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})(?:日)?", raw)
        if explicit:
            try:
                target_date = date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
                date_span = explicit.span()
            except ValueError:
                questions.append("指定日期无效，请提供正确的年月日。")

    if not schedule_type and target_date is None:
        weekday = re.search(r"(?:这周|本周|周|星期)([一二三四五六日天])", raw)
        if weekday:
            desired = _WEEKDAY_MAP[weekday.group(1)]
            delta = (desired - now.weekday()) % 7
            candidate = now.date() + timedelta(days=delta)
            candidate_dt = datetime.combine(candidate, datetime.min.time(), tzinfo=tz).replace(
                hour=int(clock[:2]), minute=int(clock[3:5]), second=int(clock[6:8])
            )
            if candidate_dt <= now:
                candidate += timedelta(days=7)
            target_date = candidate
            date_span = weekday.span()

    if not schedule_type and target_date is not None:
        hour, minute, second = (int(part) for part in clock.split(":"))
        run_at = datetime.combine(target_date, datetime.min.time(), tzinfo=tz).replace(
            hour=hour, minute=minute, second=second
        )
        schedule_type, schedule = "at", {"run_at": run_at.isoformat()}
        if date_span:
            if next_week:
                remaining = re.sub(r"下周[一二三四五六日天]", "", without_clock, count=1)
            else:
                matched_date_text = raw[date_span[0] : date_span[1]]
                remaining = without_clock.replace(matched_date_text, "", 1)

    if not schedule_type and not questions:
        questions.append("我没有识别出明确的执行时间，你希望何时执行？")

    instruction = _clean_instruction(remaining)
    if not instruction:
        questions.append("到期后具体要执行什么操作？")
    if re.search(r"(?:一个|一份|个|份)?文档", instruction) and not re.search(
        r"关于|主题|报告|总结|方案|格式|内容", instruction
    ):
        questions.append("请补充文档的主题、内容范围和期望格式。")

    result: dict[str, Any] = {
        "requires_clarification": bool(questions),
        "questions": list(dict.fromkeys(questions)),
        "instruction": instruction,
        "timezone": timezone_name,
    }
    if schedule_type:
        result["schedule_type"] = schedule_type
        result["schedule"] = schedule
        try:
            normalized = normalize_task_spec(
                {
                    "instruction": instruction or raw,
                    "schedule_type": schedule_type,
                    "schedule": schedule,
                    "timezone": timezone_name,
                },
                now_ts=now_value,
            )
            result["next_run_at"] = normalized["next_run_at"]
        except ScheduleValidationError as exc:
            result["requires_clarification"] = True
            result["questions"] = list(dict.fromkeys([*result["questions"], str(exc)]))
    return result
