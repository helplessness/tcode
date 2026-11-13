"""Offline, cross-platform regression: python -m unittest discover -s scripts/tests.

Uses spawn (also on POSIX), temporary workspaces, no credentials or services.
Kept outside the intentionally ignored private /tests directory for CI.
"""

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import hmac
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tcode_agent import file_lock
from tcode_agent.atomic_storage import atomic_write_json, file_transaction


def hold_lock(path, ready, release):
    with open(path, "a+b") as handle:
        file_lock.lock_file(handle)
        ready.set()
        release.wait(15)
        file_lock.unlock_file(handle)


def increment(path, start):
    start.wait(15)
    for _ in range(15):
        with file_transaction(path):
            value = json.loads(Path(path).read_text())
            atomic_write_json(path, value + 1)


def append_event(workdir, start):
    from tcode_agent.session.events import SessionEventStore
    start.wait(15)
    for i in range(10):
        SessionEventStore.append(session_id="session", event_type="test",
                                 idempotency_key=f"same-{i}", workdir=workdir)


def initialize_memory(workdir, ready, done):
    from tcode_agent.team.worker_memory import WorkerMemoryManager
    ready.set()
    WorkerMemoryManager._ensure("worker", workdir)
    done.set()


def append_logs(path, start):
    start.wait(15)
    for i in range(20):
        file_lock.append_locked_text(path, json.dumps({"index": i, "pid": os.getpid()}) + "\n")


class PlatformCompatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tcode-lock-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = multiprocessing.get_context("spawn")

    def start_child(self, target, args):
        child = self.ctx.Process(target=target, args=args)
        child.start()
        def cleanup():
            if child.is_alive():
                child.terminate()
            child.join(10)
            child.close()
        self.addCleanup(cleanup)
        return child

    def holder(self):
        ready, release = self.ctx.Event(), self.ctx.Event()
        path = self.root / "锁 空格.lock"
        child = self.start_child(hold_lock, (str(path), ready, release))
        self.assertTrue(ready.wait(15), "child failed to acquire lock")
        return path, child, release

    def test_nonblocking_contention_and_timeout(self):
        path, _, release = self.holder()
        with path.open("a+b") as handle:
            with self.assertRaises(BlockingIOError):
                file_lock.lock_file(handle, blocking=False)
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                file_lock.lock_file(handle, timeout=0.1)
            self.assertGreaterEqual(time.monotonic() - start, 0.09)
            release.set()
            file_lock.lock_file(handle, timeout=5)
            file_lock.unlock_file(handle)

    def test_process_death_releases_lock_without_deleting_file(self):
        path, child, _ = self.holder()
        child.terminate()
        child.join(10)
        with path.open("a+b") as handle:
            file_lock.lock_file(handle, timeout=5)
            file_lock.unlock_file(handle)
        self.assertTrue(path.exists())

    def test_concurrent_transactions_do_not_lose_updates(self):
        path = self.root / "记录.json"
        atomic_write_json(path, 0)
        start = self.ctx.Event()
        children = [self.start_child(increment, (str(path), start)) for _ in range(3)]
        start.set()
        for child in children:
            child.join(30)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(json.loads(path.read_text()), 45)

    def test_exception_releases_transaction_lock(self):
        path = self.root / "state.json"
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with file_transaction(path):
                raise RuntimeError("abort")
        with open(str(path) + ".lock", "a+b") as handle:
            file_lock.lock_file(handle, blocking=False)
            file_lock.unlock_file(handle)

    def test_lock_does_not_change_content_or_position(self):
        path = self.root / "data"
        path.write_bytes(b"example")
        with path.open("r+b", buffering=0) as handle:
            handle.seek(3)
            file_lock.lock_file(handle)
            self.assertEqual(handle.tell(), 3)
            file_lock.unlock_file(handle)
            self.assertEqual(handle.tell(), 3)
        self.assertEqual(path.read_bytes(), b"example")
        with (self.root / "empty").open("a+b") as handle:
            file_lock.lock_file(handle)
            file_lock.unlock_file(handle)
        self.assertEqual((self.root / "empty").stat().st_size, 0)

    @unittest.skipIf(os.name == "nt", "POSIX interoperability only")
    def test_existing_flock_users_still_conflict(self):
        import fcntl
        path, _, release = self.holder()
        with path.open("a+b") as handle:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release.set()

    def test_replace_failure_preserves_original(self):
        path = self.root / "state.json"
        atomic_write_json(path, {"old": True})
        with patch("tcode_agent.atomic_storage.os.replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                atomic_write_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_invalid_timeout_rejected(self):
        with (self.root / "lock").open("a+b") as handle:
            for timeout in (-1, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    file_lock.lock_file(handle, timeout=timeout)
            with self.assertRaises(ValueError):
                file_lock.lock_file(handle, blocking=False, timeout=0)

    def test_windows_adapter_contention_and_noncontention_errors(self):
        # Control-flow check only; native Windows behavior is exercised by CI.
        crt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
        with (self.root / "lock").open("a+b") as handle, \
             patch.object(file_lock, "_WINDOWS", True), \
             patch.object(file_lock, "msvcrt", crt, create=True):
            handle.seek(7)
            crt.locking.side_effect = OSError(errno.EACCES, "held")
            with self.assertRaises(BlockingIOError):
                file_lock.lock_file(handle, blocking=False)
            self.assertEqual(handle.tell(), 7)
            with self.assertRaises(TimeoutError):
                file_lock.lock_file(handle, timeout=0)
            crt.locking.side_effect = OSError(errno.EBADF, "bad descriptor")
            with self.assertRaises(OSError) as caught:
                file_lock.lock_file(handle)
            self.assertEqual(caught.exception.errno, errno.EBADF)
            crt.locking.side_effect = [OSError(errno.EACCES, "held"), None]
            file_lock.lock_file(handle, timeout=1)
            crt.locking.side_effect = None
            file_lock.unlock_file(handle)
            self.assertEqual(handle.tell(), 7)

    def test_checkpoint_roundtrip_and_stale_result(self):
        from tcode_agent.checkpoint.manager import Checkpoint, CheckpointManager
        current = Checkpoint(task_id="task", session_id="session", round_index=3,
                             step_name="completed")
        CheckpointManager.save(current, self.temp.name)
        CheckpointManager.save(Checkpoint(task_id="task", session_id="session", round_index=1), self.temp.name)
        restored = CheckpointManager.load("task", self.temp.name)
        self.assertEqual(restored.round_index, 3)
        self.assertEqual(restored.step_name, "completed")

    def test_preview_ticket_on_windows_without_getuid(self):
        from tcode_agent.server import job_workspace as preview, job_artifacts_api as api

        env = {}
        with patch.object(preview, "os", SimpleNamespace(name="nt", environ=env)), \
             patch.object(api, "_get_job_workspace_for_request", return_value={"preview_ports": [8888]}):
            result = asyncio.run(api.create_job_preview_ticket("job-a", api.JobPreviewTicketRequest(port=8888), None))
            expiry = datetime.fromisoformat(result["expires_at"]).astimezone(timezone.utc).timestamp()
            self.assertAlmostEqual(expiry - time.time(), api._PREVIEW_TICKET_TTL_SECONDS, delta=5)
            ticket = result["ticket"]
            self.assertEqual(len(preview._preview_ticket_secret()), 32)
            self.assertTrue(preview._verify_preview_ticket(ticket, "job-a", 8888))
            self.assertFalse(preview._verify_preview_ticket(ticket, "job-b", 8888))
            self.assertFalse(preview._verify_preview_ticket(ticket, "job-a", 8889))
            expired = preview._sign_preview_ticket("job-a", 8888, int(time.time()) - 1)
            self.assertFalse(preview._verify_preview_ticket(expired, "job-a", 8888))

            env.update(TCODE_PREVIEW_SECRET="shared", TCODE_JOB_PREVIEW_SECRET="job-only")
            self.assertEqual(preview._preview_ticket_secret(), hashlib.sha256(b"job-only").digest())
            self.assertFalse(preview._verify_preview_ticket(ticket, "job-a", 8888))
            del env["TCODE_JOB_PREVIEW_SECRET"]
            self.assertEqual(preview._preview_ticket_secret(), hashlib.sha256(b"shared").digest())

    def test_legacy_preview_signature_is_rejected_even_with_same_secret_and_future_expiry(self):
        from tcode_agent.server import job_workspace as preview

        expires = int(time.time()) + 8 * 3600  # Old expiry could be in the future in UTC-west zones.
        payload = f"job-a:8888:{expires}".encode()
        with patch.dict(os.environ, TCODE_JOB_PREVIEW_SECRET="fixture-shared-key"):
            sig = hmac.new(preview._preview_ticket_secret(), payload, hashlib.sha256).hexdigest()
            legacy = base64.urlsafe_b64encode(payload + b":" + sig.encode()).decode()
            self.assertFalse(preview._verify_preview_ticket(legacy, "job-a", 8888))
            self.assertTrue(preview._verify_preview_ticket(
                preview._sign_preview_ticket("job-a", 8888, int(time.time()) + 60), "job-a", 8888))

    def test_windows_custom_input_reuses_read_binding_for_upload_and_review(self):
        from tcode_agent.mcp import custom_targets as custom

        source = self.root / "sequence.fasta"
        source.write_bytes(b">test\nACD")
        target = {"origin": custom.ORIGIN, "tools": [{"name": "analyze", "files": [
            {"pointer": "/sequence", "kind": "file", "transport": "artifact"},
        ]}]}
        context = SimpleNamespace(ask=AsyncMock(return_value=True), _custom_mcp_targets={"plugin-test": target})
        current = ["ntfs-v1", 1]

        def identity(path, *, directory):
            self.assertEqual(path, source)
            self.assertFalse(directory)
            return list(current)

        @contextmanager
        def open_file(path, *, grant):
            self.assertEqual(path, source)
            self.assertEqual(grant, (str(source), ["ntfs-v1", 1], False))
            if grant[1] != current:
                raise ValueError("approved input changed")
            with path.open("rb") as stream:  # Test double, never used by real Windows uploads.
                yield stream

        with patch("tcode_agent.workspace.async_protein_run.resolve_context_input_path", return_value=source), \
             patch.object(custom, "_WINDOWS", True), \
             patch.object(custom, "_windows_input_io", return_value=(identity, open_file)):
            args = {"sequence": source.name}
            self.assertTrue(asyncio.run(custom.authorize_inputs(context, "plugin-test", "analyze", args)))
            context.ask.assert_awaited_once_with("read", [str(source)])
            saved = json.loads(json.dumps(context._custom_mcp_read_bindings))
            with custom.open_authorized_input(saved[0], source) as stream:
                self.assertEqual(stream.read(), b">test\nACD")

            current[1] = 2
            with self.assertRaisesRegex(ValueError, "原读取授权"):
                custom.validate_read_bindings(context, "plugin-test", "analyze", args, bindings=saved)
            with self.assertRaisesRegex(ValueError, "approved input changed"):
                with custom.open_authorized_input(saved[0], source):
                    self.fail("replacement must not reach upload")
            with self.assertRaisesRegex(ValueError, "不在读取授权范围"):
                with custom.open_authorized_input(saved[0], self.root / "other.fasta"):
                    self.fail("unrelated files must not reach upload")

    def test_windows_custom_input_without_sdk_fails_closed(self):
        from tcode_agent.mcp import custom_targets as custom

        source = self.root / "input"
        source.write_bytes(b"no fallback")
        with patch.object(custom, "_WINDOWS", True), patch.dict(sys.modules, {"tcode_artifacts.windows_io": None}):
            with self.assertRaisesRegex(ValueError, "安装.*Artifact SDK"):
                with custom.open_authorized_input({"path": str(source), "kind": "file", "identity": []}, source):
                    self.fail("must not fall back to ordinary open")

    def test_session_nested_lock_and_cross_process_idempotency(self):
        from tcode_agent.session.events import SessionEventStore
        with SessionEventStore._process_lock("session", workdir=self.temp.name):
            with SessionEventStore._process_lock("session", workdir=self.temp.name):
                pass
        start = self.ctx.Event()
        children = [self.start_child(append_event, (self.temp.name, start)) for _ in range(2)]
        start.set()
        for child in children:
            child.join(30)
            self.assertEqual(child.exitcode, 0)
        lines = SessionEventStore.path_for("session", workdir=self.temp.name).read_text().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual(len({json.loads(line)["idempotency_key"] for line in lines}), 10)

    def test_registry_lock_failure_does_not_enter_mutation(self):
        from tcode_agent.team.registry import WorkerRegistry
        with patch.dict(os.environ, TCODE_WORKDIR=self.temp.name), \
             patch("tcode_agent.team.registry.lock_file", side_effect=OSError(errno.EBADF, "bad")), \
             patch("tcode_agent.team.registry.unlock_file") as unlock:
            with self.assertRaises(OSError):
                with WorkerRegistry._process_file_lock():
                    self.fail("mutation must not run without the lock")
            unlock.assert_not_called()

    def test_maintenance_contention_returns_status_but_other_errors_propagate(self):
        from tcode_agent.evidence import maintenance
        from tcode_agent.evidence.store import EvidenceStore
        with patch.object(EvidenceStore, "evidence_path", return_value=self.root / "evidence.sqlite"), \
             patch.object(maintenance, "public_status", return_value={"status": "running"}) as status, \
             patch.object(maintenance, "lock_file", side_effect=BlockingIOError()) as lock:
            self.assertEqual(maintenance._run(workdir=self.temp.name), {"status": "running"})
            self.assertFalse(lock.call_args.kwargs["blocking"])
            status.reset_mock()
            lock.side_effect = OSError(errno.EBADF, "bad descriptor")
            with self.assertRaises(OSError):
                maintenance._run(workdir=self.temp.name, wait=True)
            self.assertTrue(lock.call_args.kwargs["blocking"])
            status.assert_not_called()

    def test_worker_import_without_resource_and_execution_fails_closed(self):
        code = """
import sys
sys.modules['resource'] = None
from tcode_agent.plugin_sdk.worker import _apply_resource_limits
try:
    _apply_resource_limits()
except RuntimeError as exc:
    assert 'resource limits' in str(exc)
else:
    raise AssertionError('unrestricted worker was allowed')
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=self.temp.name,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_initialization_uses_reader_transaction_lock(self):
        from tcode_agent.team.worker_memory import WorkerMemoryManager
        path = Path(WorkerMemoryManager._path("worker", self.temp.name))
        ready, done = self.ctx.Event(), self.ctx.Event()
        with file_transaction(path):
            child = self.start_child(initialize_memory, (self.temp.name, ready, done))
            self.assertTrue(ready.wait(15))
            self.assertFalse(done.wait(0.2))
            self.assertFalse(path.exists())
        child.join(15)
        self.assertEqual(child.exitcode, 0)
        self.assertEqual(json.loads(path.read_text()), [])

    def test_worker_legacy_migration_does_not_overwrite_existing_memory(self):
        from tcode_agent.team.worker_memory import WorkerMemoryManager
        legacy = WorkerMemoryManager._legacy_path("worker", self.temp.name)
        legacy.parent.mkdir(parents=True)
        legacy.write_text('[{"content": "existing"}]', encoding="utf-8")
        WorkerMemoryManager._ensure("worker", self.temp.name)
        path = Path(WorkerMemoryManager._path("worker", self.temp.name))
        self.assertEqual(path.read_text(), legacy.read_text())
        legacy.write_text("[]", encoding="utf-8")
        WorkerMemoryManager._ensure("worker", self.temp.name)
        self.assertEqual(json.loads(path.read_text()), [{"content": "existing"}])

    def test_posix_worker_resource_limits_still_applied(self):
        from tcode_agent.plugin_sdk.worker import _apply_resource_limits
        resource = SimpleNamespace(RLIMIT_CPU=0, RLIMIT_AS=1, RLIMIT_FSIZE=2,
                                   RLIMIT_NOFILE=3, RLIMIT_NPROC=4, setrlimit=Mock())
        with patch.dict(sys.modules, resource=resource):
            _apply_resource_limits()
        self.assertEqual(resource.setrlimit.call_count, 5)
        resource.setrlimit.assert_any_call(resource.RLIMIT_CPU, (30, 30))

    def test_server_import_in_isolated_workspace(self):
        result = subprocess.run(
            [sys.executable, "-c", "import tcode_agent.server.launcher"],
            cwd=self.temp.name, env={**os.environ, "TCODE_WORKDIR": self.temp.name},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_imports(self):
        for module in (
            "tcode_agent.team.worker_memory", "tcode_agent.evidence.maintenance",
            "tcode_agent.mcp.apps", "tcode_agent.runtime.command_policy",
            "tcode_agent.runtime.sandbox.workspace_snapshot", "tcode_agent.skill.trace",
            "tcode_agent.ontology_core.bundle", "tcode_agent.plugin_sdk.worker",
        ):
            with self.subTest(module=module):
                importlib.import_module(module)

    def test_state_lock_timeout_is_structured_and_private(self):
        path, _, _ = self.holder()
        with path.open("a+b") as handle:
            with self.assertRaises(file_lock.StateLockBusy) as raised:
                file_lock.lock_state_file(handle, timeout=0.05)
        self.assertEqual(raised.exception.code, "state_lock_busy")
        self.assertNotIn(self.temp.name, str(raised.exception))

    def test_filesystem_timeout_is_not_mislabeled_as_lock_contention(self):
        error = TimeoutError(errno.ETIMEDOUT, "filesystem I/O timeout")
        with patch.object(file_lock, "lock_file", side_effect=error):
            with self.assertRaises(TimeoutError) as raised:
                file_lock.lock_state_file(Mock())
        self.assertIs(raised.exception, error)

    def test_async_contention_never_sleeps_or_enters_transaction(self):
        path, _, _ = self.holder()
        async def scenario():
            with path.open("a+b") as handle, patch.object(file_lock.time, "sleep", side_effect=AssertionError("event loop blocked")):
                with self.assertRaises(file_lock.StateLockBusy):
                    file_lock.lock_state_file(handle)
            await asyncio.sleep(0)
        asyncio.run(scenario())

    def test_async_thread_guard_contention_is_nonblocking(self):
        lock = threading.Lock()
        lock.acquire()
        async def scenario():
            with self.assertRaises(file_lock.StateLockBusy):
                with file_lock.state_thread_lock(lock):
                    self.fail("must not enter")
        try:
            asyncio.run(scenario())
        finally:
            lock.release()

    def test_concurrent_log_append_preserves_all_records(self):
        path = self.root / "audit.jsonl"
        start = self.ctx.Event()
        children = [self.start_child(append_logs, (str(path), start)) for _ in range(3)]
        start.set()
        for child in children:
            child.join(30)
            self.assertEqual(child.exitcode, 0)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 60)
        self.assertEqual(len({(row["pid"], row["index"]) for row in rows}), 60)

    def test_log_sidecar_contention_leaves_log_readable_and_unchanged(self):
        path = self.root / "audit.jsonl"
        path.write_text("existing\n", encoding="utf-8")
        ready, release = self.ctx.Event(), self.ctx.Event()
        self.start_child(hold_lock, (str(path) + ".lock", ready, release))
        self.assertTrue(ready.wait(15))
        async def scenario():
            with self.assertRaises(file_lock.StateLockBusy):
                file_lock.append_locked_text(path, "new\n")
        asyncio.run(scenario())
        self.assertEqual(path.read_text(), "existing\n")

    @unittest.skipIf(os.name == "nt", "POSIX legacy writer interoperability")
    def test_log_sidecar_still_respects_legacy_data_lock(self):
        path, _, _ = self.holder()
        async def scenario():
            with self.assertRaises(file_lock.StateLockBusy):
                file_lock.append_locked_text(path, "must not write\n")
        asyncio.run(scenario())
        self.assertEqual(path.stat().st_size, 0)
        with Path(str(path) + ".lock").open("a+b") as handle:
            file_lock.lock_file(handle, blocking=False)
            file_lock.unlock_file(handle)

    def test_cancelled_session_save_settles_before_releasing_outer_guard(self):
        from tcode_agent.session.manager import SessionManager
        async def scenario():
            started, release, finished = threading.Event(), threading.Event(), threading.Event()
            guard = asyncio.Lock()
            def save(*args, **kwargs):
                started.set()
                if not release.wait(5):
                    raise AssertionError("save not released")
                finished.set()
            async def transaction():
                async with guard:
                    await SessionManager._save_to_disk_async(["fixture"])
            with patch.object(SessionManager, "_save_to_disk", side_effect=save):
                task = asyncio.create_task(transaction())
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertTrue(guard.locked())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(finished.is_set())
                self.assertFalse(guard.locked())
        asyncio.run(scenario())

    def test_session_save_error_is_not_reported_as_success(self):
        from tcode_agent.session.manager import SessionManager
        async def scenario():
            with patch.object(SessionManager, "_save_to_disk", side_effect=file_lock.StateLockBusy()):
                with self.assertRaises(file_lock.StateLockBusy):
                    await SessionManager._save_to_disk_async(["fixture"])
        asyncio.run(scenario())

    def test_busy_http_response(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tcode_agent.server._shared import state_lock_busy_response
        app = FastAPI()
        app.add_exception_handler(file_lock.StateLockBusy, state_lock_busy_response)
        @app.get("/busy")
        def busy():
            raise file_lock.StateLockBusy()
        response = TestClient(app).get("/busy")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "state_lock_busy")


if __name__ == "__main__":
    unittest.main()
