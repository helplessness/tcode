"""Lifecycle for local children started by TCode, never for persisted/remote PIDs."""

import asyncio
import os
import signal


async def start_local_process(command, *, cwd=None, stdin=None, stdout=None, stderr=None):
    """Start a shell command or a trusted configured argv in its own process group."""
    cancelled = False
    owner = asyncio.current_task()
    cancellation_count = owner.cancelling() if owner else 0

    def may_start():
        # A cancel request can be queued before the owner's exception handler runs.
        return not cancelled and (owner is None or owner.cancelling() <= cancellation_count)

    async def launch():
        options = dict(cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr)
        if os.name == "nt":
            from .windows_process import start_windows_process

            return await start_windows_process(command, may_start=may_start, **options)
        if isinstance(command, str):
            return await asyncio.create_subprocess_shell(command, start_new_session=True, **options)
        return await asyncio.create_subprocess_exec(*command, start_new_session=True, **options)

    task = asyncio.create_task(launch())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled = True
        # Spawn can finish after the caller is cancelled. Reap the owned child
        # rather than losing its handle; a second cancellation must not interrupt cleanup.
        async def cleanup():
            try:
                proc = await task
            except asyncio.CancelledError:
                return
            if not await stop_local_process(proc, grace=0, discard_output=True):
                raise RuntimeError("无法确认取消启动后的本地进程已退出")

        cleanup_task = asyncio.create_task(cleanup())
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            cleanup_task.result()
        except Exception as error:
            raise asyncio.CancelledError from error
        raise


async def stop_local_process(proc, *, grace=1.0, discard_output=False):
    """Return success only after exit confirmation, not after sending a signal."""
    # A cancelled startup/foreground capture may have no remaining reader.
    # Drain without buffering so a paused full pipe cannot prevent reaping.
    async def discard(stream):
        while await stream.read(65536):
            pass

    readers = [
        asyncio.create_task(discard(stream))
        for stream in (proc.stdout, proc.stderr) if discard_output and stream is not None
    ]
    try:
        return await _stop(proc, grace)
    finally:
        for reader in readers:
            reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)


async def _stop(proc, grace):
    if os.name == "nt":
        # WindowsProcess.wait also verifies that the Job Object is empty.
        proc.terminate_tree()
    elif proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM if grace else signal.SIGKILL)
        except ProcessLookupError:
            pass
        if grace:
            try:
                await asyncio.wait_for(proc.wait(), grace)
                return True
            except asyncio.TimeoutError:
                # Never signal a possibly recycled group ID after its leader was reaped.
                if proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    return False
    try:
        await asyncio.wait_for(proc.wait(), 5.0)
        return True
    except asyncio.TimeoutError:
        return False
