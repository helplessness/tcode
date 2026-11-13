"""Manual cross-platform tests; all processes are disposable local fixtures."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tcode_agent.runtime import local_process


PIPE = asyncio.subprocess.PIPE
WINDOWS = os.name == "nt"


def test_argv_cwd_and_stdio_are_preserved(tmp_path):
    cwd = tmp_path / "目录 with spaces"
    cwd.mkdir()
    argv = ["with spaces", '"quote"', "中文", "literal & value"]

    async def run():
        proc = await local_process.start_local_process(
            [sys.executable, "-c",
             "import json, os, sys; print(json.dumps([os.path.basename(os.getcwd()), sys.argv[1:], sys.stdin.buffer.read().hex()]))",
             *argv], cwd=cwd, stdin=PIPE, stdout=PIPE, stderr=PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(b"\x01payload\x00"), 10)
            assert proc.returncode == 0, err
            assert json.loads(out) == [cwd.name, argv, b"\x01payload\x00".hex()]
        finally:
            await local_process.stop_local_process(proc, grace=0, discard_output=True)

    asyncio.run(run())


def test_cancelled_spawn_is_reaped_even_when_cancelled_twice(monkeypatch):
    async def run():
        entered, ready, cleaning, finish = (asyncio.Event() for _ in range(4))
        child = object()

        async def spawn(*args, **kwargs):
            entered.set()
            await ready.wait()
            return child

        async def stop(proc, **kwargs):
            assert proc is child
            cleaning.set()
            await finish.wait()
            return True

        monkeypatch.setattr(local_process, "os", SimpleNamespace(name="posix"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(local_process, "stop_local_process", stop)
        task = asyncio.create_task(local_process.start_local_process(["fixture"]))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        ready.set()
        await asyncio.wait_for(cleaning.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_stop_is_not_reported_as_terminated_before_confirmation(monkeypatch):
    from tcode_agent.tools.builtin import process_runtime

    async def run():
        cls = process_runtime.ProcessRuntime
        proc = SimpleNamespace(returncode=None)
        managed = process_runtime.ManagedProcess("p", "fixture", None, proc=proc)
        monkeypatch.setattr(cls, "get", AsyncMock(return_value=managed))
        monkeypatch.setattr(cls, "_lock", asyncio.Lock())
        monkeypatch.setattr(cls, "_save", lambda: None)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def not_confirmed(proc):
            entered.set()
            await finish.wait()
            return False

        monkeypatch.setattr(process_runtime, "stop_local_process", not_confirmed)
        task = asyncio.create_task(cls.terminate("p"))
        await asyncio.wait_for(entered.wait(), 3)
        assert managed.status == "terminating"
        assert managed.finished_at is None
        finish.set()
        assert not await task
        assert managed.status == "terminating"
        proc.returncode = 1
        monkeypatch.setattr(process_runtime, "stop_local_process", AsyncMock(return_value=True))
        assert await cls.terminate("p")
        assert managed.status == "terminated"
        assert managed.returncode == 1

    asyncio.run(run())


def test_windows_host_shell_cannot_use_posix_enforcement(monkeypatch, tmp_path):
    from tcode_agent.runtime import command_policy, sandbox
    from tcode_agent.tools.base import ToolContext

    monkeypatch.setattr(command_policy, "_WINDOWS", True)
    monkeypatch.setattr(sandbox, "tool_should_run_in_sandbox", lambda *_: False)
    context = ToolContext("test", "test", "test", None, extra={"workdir": str(tmp_path)})
    decision = command_policy.evaluate_command_policy(
        "echo %UNTRUSTED_VAR%", context=context, tool_name="bash",
    )
    assert not decision.allowed
    assert decision.code == "command.denied.windows_host_shell"
    audit = command_policy.evaluate_command_policy(
        "echo %UNTRUSTED_VAR%", context=context, tool_name="exec",
        policy_override={"mode": "audit"},
    )
    assert audit.allowed
    assert audit.categories == ("unclassified",)


def test_registration_failure_does_not_leave_a_running_child(monkeypatch, tmp_path):
    from tcode_agent.tools.builtin import process_runtime

    async def run():
        cls = process_runtime.ProcessRuntime
        child = await local_process.start_local_process(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path,
        )
        monkeypatch.delenv("TCODE_DESKTOP_MANAGED", raising=False)
        monkeypatch.setattr(cls, "_processes", {})
        monkeypatch.setattr(cls, "_loaded", True)
        monkeypatch.setattr(cls, "_lock", asyncio.Lock())
        monkeypatch.setattr(cls, "_save", lambda: (_ for _ in ()).throw(OSError("fixture save failure")))
        monkeypatch.setattr(process_runtime, "start_local_process", AsyncMock(return_value=child))
        try:
            with pytest.raises(OSError, match="fixture save failure"):
                await cls.start("fixture", str(tmp_path))
            assert child.returncode is not None
            assert not cls._processes
        finally:
            await local_process.stop_local_process(child, grace=0, discard_output=True)

    asyncio.run(run())


def test_bash_timeout_reaps_local_process(monkeypatch, tmp_path):
    from tcode_agent.tools.base import ToolContext
    from tcode_agent.tools.builtin import bash

    monkeypatch.setattr(bash, "tool_should_run_in_sandbox", lambda *_: False)
    monkeypatch.setattr(bash, "authorize_command", AsyncMock(
        return_value=SimpleNamespace(allowed=True, workdir=str(tmp_path)),
    ))
    started = []

    async def start(*args, **kwargs):
        proc = await local_process.start_local_process(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(bash, "start_local_process", start)
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    command = subprocess.list2cmdline(argv) if WINDOWS else shlex.join(argv)

    async def run():
        context = ToolContext("test", "test", "test", None, extra={"workdir": str(tmp_path)})
        try:
            result = await asyncio.wait_for(
                bash.BashTool().execute({"command": command, "timeout": 1}, context), 12,
            )
            assert result.error == "Command timed out"
            assert len(started) == 1 and started[0].returncode is not None
        finally:
            for proc in started:
                await local_process.stop_local_process(proc, grace=0, discard_output=True)

    asyncio.run(run())


def test_local_stdio_disconnect_does_not_stop_another_service(monkeypatch):
    from tcode_agent.mcp import client
    from tcode_agent.mcp.config import MCPConfig, MCPType

    monkeypatch.delenv("TCODE_DESKTOP_MANAGED", raising=False)
    spawn = AsyncMock(wraps=local_process.start_local_process)
    monkeypatch.setattr(client, "start_local_process", spawn)

    async def run():
        invalid = client.StdioMCPClient(MCPConfig("invalid", MCPType.LOCAL, command="echo not argv"))
        assert not await invalid.connect()
        spawn.assert_not_awaited()
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        first, second = [client.StdioMCPClient(MCPConfig(name, MCPType.LOCAL, command=command))
                         for name in ("first", "second")]
        try:
            assert await asyncio.gather(first.connect(), first.connect()) == [True, True]
            assert spawn.await_count == 1
            assert await second.connect()
            proc = first._process
            assert await first.connect() and first._process is proc
            await first.disconnect()
            assert proc.returncode is not None and not first.is_connected
            assert second._process.returncode is None
        finally:
            await first.disconnect()
            await second.disconnect()

    asyncio.run(run())


@pytest.mark.skipif(not WINDOWS, reason="requires native Windows Job Objects")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_windows_reaps_descendant_on_stop_or_leader_exit(leader_exits):
    import ctypes
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    script = (
        "import subprocess, sys; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=sys.stdout); "
        "print(p.pid, flush=True); sys.stdin.buffer.read(1)"
    )

    async def run():
        proc = await local_process.start_local_process(
            [sys.executable, "-c", script], stdin=PIPE, stdout=PIPE, stderr=PIPE,
        )
        child = None
        try:
            pid = int(await asyncio.wait_for(proc.stdout.readline(), 10))
            child = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
            assert child and kernel.WaitForSingleObject(child, 0) == 258
            if leader_exits:
                proc.stdin.write(b"x")
                await proc.stdin.drain()
                assert await asyncio.wait_for(proc.wait(), 10) == 0
            else:
                assert await local_process.stop_local_process(proc)
            assert kernel.WaitForSingleObject(child, 0) == 0
        finally:
            if child:
                kernel.CloseHandle(child)
            await local_process.stop_local_process(proc, grace=0, discard_output=True)

    asyncio.run(run())


@pytest.mark.skipif(not WINDOWS, reason="requires native Windows Job Objects")
def test_windows_assignment_failure_never_releases_command(monkeypatch, tmp_path):
    from tcode_agent.runtime import windows_process

    marker = tmp_path / "must-not-exist"
    monkeypatch.setattr(windows_process, "_assign", lambda *_: False)
    with pytest.raises(OSError):
        asyncio.run(local_process.start_local_process(
            [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)],
        ))
    assert not marker.exists()
