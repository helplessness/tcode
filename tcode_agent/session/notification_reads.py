"""UI read receipts, independent of task snapshots and their persistence lock."""

from contextlib import closing

from .store_sqlite import SessionSQLiteStore
from ..sqlite_runtime import connect


def _connect():
    path = SessionSQLiteStore.db_path().with_name("notification-reads.sqlite")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(str(path), timeout=2)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS reads (session_id TEXT, event_id TEXT, "
        "read_at TEXT NOT NULL, PRIMARY KEY(session_id, event_id))"
    )
    return connection


def apply_reads(session_id, events):
    if not events:
        return events
    with closing(_connect()) as connection:
        receipts = dict(connection.execute(
            "SELECT event_id, read_at FROM reads WHERE session_id = ?", (session_id,)
        ))
    return [{**event, "read_at": receipts.get(str(event.get("id", "")), event.get("read_at", ""))}
            for event in events]


def record_reads(events):
    if not events:
        return 0
    with closing(_connect()) as connection, connection:
        before = connection.total_changes
        connection.executemany("INSERT OR IGNORE INTO reads VALUES (?, ?, ?)", events)
        return connection.total_changes - before
