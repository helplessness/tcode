"""Windows CPython Job Object backend for owned local subprocesses (not a sandbox)."""

import asyncio
import ctypes as c
from ctypes import wintypes as w
import os
import sys


class _BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
        ("working_set_min", c.c_size_t), ("working_set_max", c.c_size_t),
        ("active_limit", w.DWORD), ("affinity", c.c_size_t),
        ("priority", w.DWORD), ("scheduling", w.DWORD),
    ]


class _ExtendedLimits(c.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", c.c_uint64 * 6),
        ("memory_limits_and_peaks", c.c_size_t * 4),
    ]


class _Accounting(c.Structure):
    _fields_ = [
        ("times", c.c_int64 * 4), ("page_faults", w.DWORD),
        ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD),
    ]


_kernel = c.WinDLL("kernel32", use_last_error=True)


def _api(name, args, result=w.BOOL):
    method = getattr(_kernel, name)
    method.argtypes, method.restype = args, result
    return method


_create = _api("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE)
_set = _api("SetInformationJobObject", [w.HANDLE, c.c_int, w.LPVOID, w.DWORD])
_query = _api("QueryInformationJobObject", [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, w.LPVOID])
_assign = _api("AssignProcessToJobObject", [w.HANDLE, w.HANDLE])
_terminate = _api("TerminateJobObject", [w.HANDLE, w.UINT])
_close = _api("CloseHandle", [w.HANDLE])


def _check(result):
    if not result:
        raise c.WinError(c.get_last_error())


class _Job:
    def __init__(self):
        self.handle = _create(None, None)  # unnamed and non-inheritable
        _check(self.handle)
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            _check(_set(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def active(self):
        info = _Accounting()
        _check(_query(self.handle, 1, c.byref(info), c.sizeof(info), None))
        return info.active

    def terminate(self):
        if self.handle and self.active():
            _check(_terminate(self.handle, 1))

    def close(self):
        if self.handle:
            _check(_close(self.handle))
            self.handle = None


# A trusted Python bootstrap blocks before executing any user command. stdin's
# first byte is the gate, the rest remains the tool's unchanged stdio stream.
# -I -S prevents workspace modules/site hooks from running before assignment.
_BOOTSTRAP = r"""
import os, subprocess, sys
import msvcrt
msvcrt.setmode(0, os.O_BINARY)
if os.read(0, 1) != b'\x01':
    raise SystemExit(125)
args = sys.argv[3:]
stdio = dict(cwd=sys.argv[2], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
if sys.argv[1] == 'shell':
    shell = os.path.join(os.environ['SystemRoot'], 'System32', 'cmd.exe')
    args = f'"{shell}" /d /s /c "{args[0]}"'
    raise SystemExit(subprocess.call(args, executable=shell, **stdio))
raise SystemExit(subprocess.call(args, **stdio))
"""


class _ExitProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    def __init__(self, loop):
        super().__init__(limit=65536, loop=loop)
        self.exited = asyncio.Event()

    def process_exited(self):
        self.exited.set()  # leader exit does not wait for descendants' pipe EOF
        super().process_exited()


class _WindowsProcess(asyncio.subprocess.Process):
    def __init__(self, transport, protocol, loop, job):
        super().__init__(transport, protocol, loop)
        self._job = job
        self._cleanup = loop.create_task(self._reap_tree(protocol.exited))
        # Retain the error for wait() without an unhandled-task warning when
        # a stdio server exits before its owner calls disconnect.
        self._cleanup.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    def terminate_tree(self):
        self._job.terminate()

    async def _reap_tree(self, exited):
        try:
            await exited.wait()
            self._job.terminate()
            async with asyncio.timeout(4.0):
                while self._job.active():
                    await asyncio.sleep(0.025)
        finally:
            self._job.close()

    async def wait(self):
        await asyncio.shield(self._cleanup)
        return await super().wait()


async def start_windows_process(command, *, cwd, stdin, stdout, stderr, may_start):
    if getattr(sys, "frozen", False) or sys.implementation.name != "cpython":
        raise RuntimeError("Windows 本地进程管理需要源码或虚拟环境中的 CPython")
    if stdin not in (None, asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL):
        raise ValueError("Windows 本地进程仅支持管道或关闭的 stdin")
    loop = asyncio.get_running_loop()
    shell = isinstance(command, str)
    args = [command] if shell else list(command)
    if not args or not args[0]:
        raise ValueError("本地进程命令不能为空")
    workdir = os.path.abspath(cwd or os.getcwd())
    if shell and workdir.startswith("\\\\"):
        raise RuntimeError("Windows 本地 shell 需要盘符工作目录，不能使用 UNC 或设备路径")
    job = _Job()
    transport = None
    try:
        transport, protocol = await loop.subprocess_exec(
            lambda: _ExitProtocol(loop),
            sys.executable, "-I", "-S", "-c", _BOOTSTRAP, "shell" if shell else "exec",
            workdir, *args,
            # Do not start the pre-assignment interpreter in an untrusted workspace.
            cwd=os.path.dirname(sys.executable), stdin=asyncio.subprocess.PIPE,
            stdout=stdout, stderr=stderr,
        )
        # CPython's Popen keeps the original CreateProcess handle. Do not
        # reopen a PID (it may already have exited and been reused).
        popen = transport.get_extra_info("subprocess")
        handle = getattr(popen, "_handle", None)
        if not handle:
            raise RuntimeError("无法取得 Windows 启动进程的原始句柄，命令未执行")
        _check(_assign(job.handle, int(handle)))
        if not may_start():
            raise asyncio.CancelledError
        protocol.stdin.write(b"\x01")
        if stdin != asyncio.subprocess.PIPE:
            protocol.stdin.close()
        return _WindowsProcess(transport, protocol, loop, job)
    except BaseException:
        job.close()  # also kills the bootstrap if assignment succeeded
        if transport is not None:
            # close kills a still-unassigned bootstrap and closes its pipes.
            transport.close()
            await asyncio.wait_for(asyncio.subprocess.Process(transport, protocol, loop).wait(), 5)
        raise
