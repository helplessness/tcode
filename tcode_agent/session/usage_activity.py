"""Daily token activity from timestamped canonical session events."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1024)
def _file_activity(path: str, size: int, mtime_ns: int, utc_offset_minutes: int):
    del size, mtime_ns  # Only used to invalidate the cached result after an append.
    zone = timezone(timedelta(minutes=utc_offset_minutes))
    seen: dict[str, tuple[int, int, int]] = {}
    days = defaultdict(lambda: [0, 0, 0])
    with Path(path).open("rb") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if event.get("event_type") == "verification.model.finished":
                    payload = event.get("payload") or {}
                    if payload.get("included_in_session_usage"):
                        continue
                    event = {**event, "event_type": "session.patch", "payload": {"messages": [
                        {"id": "verification:" + str(payload.get("call_id", "")), "usage": payload.get("usage")}]}}
                if event.get("event_type") != "session.patch":
                    continue
                timestamp = event.get("created_at")
                messages = event.get("payload", {}).get("messages", [])
                if not isinstance(timestamp, (int, float)) or not isinstance(messages, list):
                    continue
                day = datetime.fromtimestamp(timestamp, zone).date().isoformat()
                for message in messages:
                    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                        continue
                    message_id = message.get("id")
                    if not message_id:
                        continue
                    usage = message["usage"]
                    current = tuple(max(0, int(usage.get(key) or 0)) for key in (
                        "total_tokens", "prompt_tokens", "completion_tokens"
                    ))
                    previous = seen.get(message_id, (0, 0, 0))
                    delta = tuple(max(0, now - old) for now, old in zip(current, previous))
                    seen[message_id] = tuple(max(now, old) for now, old in zip(current, previous))
                    for index, value in enumerate(delta):
                        days[day][index] += value
            except (ValueError, TypeError, OverflowError):
                continue  # A partially written or legacy event must not hide valid days.
    return tuple((day, *values) for day, values in days.items())


def build_usage_activity(paths: list[Path], utc_offset_minutes: int, *, today: str) -> dict:
    """Aggregate only dated patches; snapshots cannot identify the day of past usage."""
    cutoff = (datetime.fromisoformat(today).date() - timedelta(days=365)).isoformat()
    totals = defaultdict(lambda: [0, 0, 0])
    for path in paths:
        try:
            stat = path.stat()
            records = _file_activity(str(path), stat.st_size, stat.st_mtime_ns, utc_offset_minutes)
        except OSError:
            continue
        for day, total, prompt, completion in records:
            if cutoff <= day <= today:
                values = totals[day]
                values[0] += total
                values[1] += prompt
                values[2] += completion
    return {"days": [
        {"date": day, "total_tokens": values[0], "prompt_tokens": values[1], "completion_tokens": values[2]}
        for day, values in sorted(totals.items())
    ]}
