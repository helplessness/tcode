"""Offline, single-process Seatbelt jobs over private workspace copies.

No fork is granted: a trusted supervisor can reap the sole execution PID even
after exec/setsid, and EOF on its control pipe handles backend death.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import stat
import subprocess
import socket
import sys
import tempfile
import uuid

from .base import SandboxJob, SandboxJobStatus, SandboxResult, SandboxSpec

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000
MAX_OUTPUT = 512 * 1024
_jobs: dict[str, dict] = {}
_starting = 0

# Supervisor is trusted and has no task imports. Task stdin never receives the
# control channel or credentials. Limits apply only to the sandboxed child.
_SUPERVISOR = r'''
import json, os, resource, selectors, signal, subprocess, sys, time
cfg=json.loads(sys.stdin.readline())
def limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0,0))
    resource.setrlimit(resource.RLIMIT_CPU, (cfg['timeout'],cfg['timeout']))
    resource.setrlimit(resource.RLIMIT_FSIZE, (67108864,67108864))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128,128))
child=subprocess.Popen(cfg['argv'], cwd=cfg['cwd'], env=cfg['env'], stdin=subprocess.DEVNULL,
                       close_fds=True, start_new_session=True, preexec_fn=limits)
def stop(*args):
    if child.poll() is None: child.kill()
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
poll=selectors.DefaultSelector(); poll.register(sys.stdin,selectors.EVENT_READ)
deadline=time.monotonic()+cfg['timeout']
next_check=0
while child.poll() is None:
    if time.monotonic() >= deadline:
        stop(); child.wait(); sys.exit(124)
    if time.monotonic() >= next_check:
        next_check=time.monotonic()+.2
        total=0; count=0
        for parent,dirs,files in os.walk(cfg['root'],followlinks=False):
            count+=len(dirs)+len(files)
            for name in files:
                try: total+=os.lstat(os.path.join(parent,name)).st_size
                except FileNotFoundError: pass
            if count>4000 or total>134217728:
                stop(); child.wait(); sys.exit(126)
    if poll.select(.1) and not os.read(sys.stdin.fileno(),1024):
        stop(); child.wait(); sys.exit(125)
sys.exit(child.returncode if child.returncode >= 0 else 128-child.returncode)
'''


def _literal(path: Path | str) -> str:
    return json.dumps(str(path), ensure_ascii=True)


def profile(
    root: Path,
    *,
    read_only: Path | None = None,
    extra_read: tuple[Path, ...] = (),
    extra_write: tuple[Path, ...] = (),
) -> str:
    # No /Users, whole project, Homebrew prefix, blanket read or Mach service grant.
    roots = {Path('/bin'), Path('/usr/bin'), Path('/usr/lib'), Path('/System/Library'),
             Path('/System/Volumes/Preboot/Cryptexes/OS'), Path(sys.base_prefix).resolve(),
             (Path(sys.prefix) / 'bin').resolve(), (Path(sys.prefix) / 'lib').resolve(),
             root.resolve(), *[path.resolve() for path in extra_read],
             *[path.resolve() for path in extra_write]}
    reads = ' '.join(f'(subpath {_literal(path)})' for path in sorted(roots))
    venv_config = Path(sys.prefix) / 'pyvenv.cfg'
    if venv_config.is_file():
        reads += f' (literal {_literal(venv_config)})'
    return '\n'.join([
        '(version 1)', '(deny default)', '(allow process-exec)',
        '(allow sysctl-read)', '(allow file-read-metadata)',
        f'(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal {_literal(Path(sys.executable).resolve())}) {reads})',
        f'(allow file-write* (literal "/dev/null") (subpath {_literal(root.resolve())}) '
        + ' '.join(f'(subpath {_literal(path.resolve())})' for path in extra_write) + ')',
        *([f'(deny file-write* (subpath {_literal(read_only.resolve())}))'] if read_only else []),
        '(deny network*)', '(deny process-fork)',
    ])


def probe() -> bool:
    """Exercise real positive and negative operations, not profile text alone."""
    if sys.platform != 'darwin':
        return False
    try:
        with tempfile.TemporaryDirectory(prefix='tcode-seatbelt-') as directory, socket.socket() as server:
            parent = Path(directory).resolve()
            work = parent / 'work'; work.mkdir()
            secret = parent / 'fake-secret'; secret.write_text('probe-only')
            server.bind(('127.0.0.1', 0)); server.listen()
            with socket.create_connection(server.getsockname(), timeout=1): pass
            script = '''import errno,os,socket,sys
from pathlib import Path
Path('allowed').write_text('ok')
assert Path('allowed').read_text()=='ok'
for action in (lambda:Path(sys.argv[1]).read_text(),lambda:Path(sys.argv[1]).write_text('bad'),lambda:socket.create_connection(('127.0.0.1',int(sys.argv[2])),timeout=1),os.fork):
 try: action()
 except OSError as exc: assert exc.errno in (errno.EPERM,errno.EACCES),str(exc)
 else: raise AssertionError('sandbox boundary failed')
print('seatbelt-ok')'''
            result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile(work), str(Path(sys.executable).resolve()), '-I', '-S', '-c', script, str(secret), str(server.getsockname()[1])],
                cwd=work, env={'PATH':'/usr/bin:/bin', 'HOME':str(work)}, capture_output=True, text=True, timeout=5, close_fds=True)
            return result.returncode == 0 and result.stdout.strip() == 'seatbelt-ok' and secret.read_text() == 'probe-only'
    except (OSError, subprocess.SubprocessError):
        return False


def _excluded(name: str) -> bool:
    return name.startswith('.') or name in {'node_modules', '__pycache__', 'config', 'mcp.json', 'plugins.json', 'tcode.json'}


def _registered_agent_workspace(expected: Path, workspace: Path, session_id: str) -> bool:
    try:
        relative = workspace.relative_to(expected)
        if len(relative.parts) == 3 and relative.parts[:2] == ('.tcode', 'agent-snapshots'):
            strategy = 'snapshot'
        elif len(relative.parts) == 4 and relative.parts[:3] == ('.tcode', 'runs', 'protein'):
            strategy = 'protein_run'
        else:
            return False
        metadata_path = expected / '.tcode' / 'agent-workspaces' / f'{relative.parts[-1]}.json'
        if metadata_path.is_symlink():
            return False
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        return (
            metadata.get('strategy') == strategy
            and metadata.get('isolated') is True
            and bool(session_id)
            and str(metadata.get('session_id') or '') == str(session_id or '')
            and Path(str(metadata.get('control_workdir') or '')).resolve(strict=True) == expected
            and Path(str(metadata.get('execution_workdir') or '')).resolve(strict=True) == workspace
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def copy_tree(source: Path, destination: Path, *, relative_paths: set[str] | None = None) -> dict[str, str]:
    """Copy using directory descriptors; reject links/devices and bound inputs."""
    hashes: dict[str, str] = {}
    total = 0
    entries = 0
    def visit(fd: int, target: Path, relative: Path):
        nonlocal total, entries
        if len(relative.parts) > 32:
            raise ValueError('本地执行输入目录超过 32 层')
        for name in sorted(os.listdir(fd)):
            if _excluded(name):
                continue
            rel = relative / name
            if relative_paths is not None and not any(
                item == rel.as_posix() or item.startswith(rel.as_posix() + '/')
                for item in relative_paths
            ):
                continue
            entries += 1
            if entries > 4000:
                raise ValueError('本地执行输入目录项过多')
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if relative_paths is None:
                        (target / name).mkdir()
                    visit(child, target / name, rel)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if len(hashes) >= MAX_FILES or total + info.st_size > MAX_BYTES:
                    raise ValueError('本地执行输入超过 2000 个文件或 64 MiB，请使用较小工作区')
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    actual = os.fstat(child)
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                        raise ValueError('文件身份在读取前发生变化')
                    with os.fdopen(child, 'rb', closefd=False) as stream:
                        data = stream.read(MAX_BYTES - total + 1)
                    total += len(data)
                    if total > MAX_BYTES:
                        raise ValueError('本地执行输入超过 64 MiB')
                    digest = hashlib.sha256(data).hexdigest()
                    if relative_paths is None:
                        (target / name).write_bytes(data)
                    else:
                        existing = destination / rel
                        publish_file(destination, rel.as_posix(), data,
                                     expected=digest if existing.exists() else None)
                    hashes[rel.as_posix()] = digest
                finally:
                    os.close(child)
            else:
                raise ValueError(f'本地隔离不接收符号链接、硬链接或特殊文件：{rel}')
    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(fd, destination, Path())
    finally:
        os.close(fd)
    return hashes


def publish_file(root: Path, relative: str, data: bytes, *, expected: str | None = None) -> None:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {'.', '..'} or _excluded(p) for p in parts):
        raise ValueError('不允许写入管理文件或工作区之外')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temp = '.native-' + uuid.uuid4().hex
    try:
        for part in parts[:-1]:
            try: os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError: pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        try:
            old = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            old = None
        if old is not None:
            try:
                if not stat.S_ISREG(os.fstat(old).st_mode):
                    raise ValueError('输出目标不是普通文件')
                with os.fdopen(old, 'rb', closefd=False) as stream:
                    previous = hashlib.sha256(stream.read(MAX_BYTES + 1)).hexdigest()
                if expected is None or previous != expected:
                    raise ValueError('目标文件已变化，保留原文件，请重试')
            finally: os.close(old)
        elif expected is not None:
            raise ValueError('目标文件已移除，请重试')
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        if expected is None:
            os.link(temp, parts[-1], src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        else:
            os.replace(temp, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try: os.unlink(temp, dir_fd=fd)
        except FileNotFoundError: pass
        os.close(fd)


class MacOSNativeRuntime:
    async def start_job(self, spec: SandboxSpec) -> SandboxJob:
        global _starting
        if _starting + sum(not j['done'].done() for j in _jobs.values()) >= 2:
            raise RuntimeError('已有两个本地任务，请稍后重试')
        _starting += 1
        try:
            return await self._start_job(spec)
        finally:
            _starting -= 1

    async def _start_job(self, spec: SandboxSpec) -> SandboxJob:
        if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
            raise RuntimeError('macOS 原生隔离执行器不可用')
        if spec.network != 'none':
            raise ValueError('本地隔离仅支持断网及受检工作区/技能副本，不接受外部挂载')
        if spec.tool_name not in {'read', 'write', 'edit', 'glob', 'grep'} and spec.env.keys() - {'TALL_WORKSPACE'}:
            raise ValueError('本地执行不接受任意环境变量或代理配置')
        skill_parts = shlex.split(spec.command) if spec.tool_name == 'skill_run' else []
        if spec.tool_name == 'skill_run' and not any(item.startswith('/skill/') for item in skill_parts):
            raise ValueError('技能命令缺少已声明的入口文件')
        skill_source = None
        if spec.tool_name == 'skill_run':
            skill_source = Path(str(spec.metadata.get('desktop_skill_source_dir') or '')).expanduser()
            if not skill_source.is_absolute() or not (skill_source / 'SKILL.md').is_file():
                raise ValueError('技能来源无效')
        # Retain a bounded recent history; published artifacts live in workspace.
        for key in list(_jobs)[:-32]:
            old = _jobs[key]
            if old['done'].done():
                shutil.rmtree(old['work'].parent, ignore_errors=True)
                del _jobs[key]
        from ...server.workdirs import resolve_user_workdir_path, resolve_workspace_context, assert_workspace_active
        owner = {key: str(spec.metadata.get(key) or '') for key in ('tenant_id', 'user_id', 'workspace_id')}
        if not all(owner.values()):
            raise ValueError('本地执行缺少可信工作区身份')
        server_root = os.environ['TCODE_WORKDIR']
        expected = resolve_user_workdir_path(server_root, owner['workspace_id'], tenant_id=owner['tenant_id'], user_id=owner['user_id']).resolve()
        workspace = spec.pathmap.host_workspace.resolve(strict=True)
        relative_workspace = workspace.relative_to(expected) if workspace.is_relative_to(expected) else None
        registered_workspace = _registered_agent_workspace(
            expected,
            workspace,
            str(spec.metadata.get('session_id') or ''),
        )
        if relative_workspace is None or (
            any(_excluded(part) for part in relative_workspace.parts)
            and not registered_workspace
        ):
            raise ValueError('本地执行工作区与身份不匹配')
        # A registered run declares its own inputs as a read-only mount. Those
        # files are already in the bounded private copy; no host mount is needed.
        inputs = workspace / 'inputs'
        for mount in spec.mounts:
            if not (
                registered_workspace and mount.read_only
                and mount.source.resolve(strict=True) == inputs
                and mount.target == spec.pathmap.container_workspace.rstrip('/') + '/inputs'
            ):
                raise ValueError('本地隔离不接受外部挂载')
        assert_workspace_active(server_root, resolve_workspace_context(**owner))
        relative_cwd = spec.host_cwd.resolve(strict=True).relative_to(workspace)
        identifier = 'native-' + uuid.uuid4().hex
        root = Path(server_root).resolve() / '.tcode/native-jobs' / identifier
        root.mkdir(parents=True, mode=0o700)
        manifest = root.with_suffix('.json')
        with manifest.open('x') as handle:
            os.chmod(manifest, 0o600)
            json.dump(owner, handle)
        work = root / 'work'; work.mkdir()
        # ponytail: bounded whole-workspace copies; use per-operation snapshots
        # when workspaces larger than 64 MiB need native processing.
        try:
            hashes = copy_tree(workspace, work)
            if skill_source is not None:
                skill = root / 'skill'; skill.mkdir()
                copy_tree(skill_source, skill)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            manifest.unlink(missing_ok=True)
            raise
        (root / 'tmp').mkdir(); (root / 'home').mkdir()
        cwd = work / relative_cwd
        if not cwd.is_dir():
            raise ValueError('工作目录未包含在本地任务副本中')
        env = {'PATH': f'{Path(sys.prefix).resolve()}/bin:/usr/bin:/bin', 'HOME': str(root / 'home'),
               'TMPDIR': str(root / 'tmp'), 'LANG': 'en_US.UTF-8', 'TALL_WORKSPACE': str(work)}
        if spec.tool_name in {'read', 'write', 'edit', 'glob', 'grep'}:
            env['TCODE_FILE_IO_REQUEST'] = spec.env['TCODE_FILE_IO_REQUEST']
        command = spec.command
        if spec.tool_name == 'skill_run':
            # SkillRunTool created this with shlex.join; keep every validated
            # argument as data and translate only virtual path prefixes.
            parts = [str(work) + item[len('/workspace'):] if item == '/workspace' or item.startswith('/workspace/')
                     else str(skill) + item[len('/skill'):] if item == '/skill' or item.startswith('/skill/')
                     else item for item in skill_parts]
            runtime_reads = ()
            if parts[0] == 'node':
                node = Path(os.environ.get('TCODE_DESKTOP_NODE', ''))
                if not node.is_absolute() or not node.is_file() or node.parent.name != 'MacOS':
                    raise ValueError('桌面 Node 运行时不可用，请通过 TCode 桌面启动后重试')
                node = node.resolve()
                parts[0] = str(node)
                env['ELECTRON_RUN_AS_NODE'] = '1'
                runtime_reads = (node, node.parent.parent / 'Frameworks')
            job_profile = profile(root, read_only=skill, extra_read=runtime_reads)
            argv = ['/usr/bin/sandbox-exec', '-p', job_profile, *parts]
        else:
            job_profile = profile(root)
            argv = ['/usr/bin/sandbox-exec', '-p', job_profile, '/bin/bash', '--noprofile', '--norc', '-c', command]
        if spec.mounts:
            argv[2] += f'\n(deny file-write* (subpath {_literal(work / "inputs")}))'
        proc = await asyncio.create_subprocess_exec(sys.executable, '-I', '-S', '-c', _SUPERVISOR,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}, close_fds=True, start_new_session=True)
        try:
            assert_workspace_active(server_root, resolve_workspace_context(**owner))
            if not root.is_dir():
                raise ValueError('工作区执行已清理')
        except Exception:
            proc.stdin.close()
            await proc.communicate()
            raise
        proc.stdin.write((json.dumps({'argv': argv, 'cwd': str(cwd), 'root':str(root), 'env': env, 'timeout': min(120, max(1, spec.timeout_seconds))}) + '\n').encode())
        await proc.stdin.drain()
        job = SandboxJob(identifier, '', '', command, metadata={'backend': 'macos-seatbelt', 'network': 'disabled', 'single_process': True, 'workspace_copy': str(work), **owner})
        state = {'job': job, 'proc': proc, 'stdout': bytearray(), 'stderr': bytearray(), 'exit_code': None,
                 'work': work, 'workspace': workspace, 'hashes': hashes, 'spec': spec}
        async def drain(stream, output):
            while chunk := await stream.read(16384):
                if len(output) + len(chunk) > MAX_OUTPUT:
                    job.metadata['output_truncated'] = True
                output.extend(chunk[:max(0, MAX_OUTPUT-len(output))])
        async def finish():
            await asyncio.gather(drain(proc.stdout, state['stdout']), drain(proc.stderr, state['stderr']), proc.wait())
            state['exit_code'] = proc.returncode
            if job.status == 'running':
                job.status = 'completed' if proc.returncode == 0 else 'timed_out' if proc.returncode == 124 else 'failed'
            if job.status == 'completed':
                try:
                    assert_workspace_active(server_root, resolve_workspace_context(**owner))
                except Exception:
                    job.status = 'failed'
                    state['stderr'].extend('工作区已停用，结果未发布'.encode())
            if job.status == 'completed' and spec.tool_name not in {'read', 'write', 'edit', 'glob', 'grep'}:
                try:
                    exported = root / 'export'; exported.mkdir()
                    result_hashes = copy_tree(work, exported)
                    files = []
                    for rel, digest in result_hashes.items():
                        if hashes.get(rel) != digest:
                            destination = f'artifacts/local/{identifier}/{rel}'
                            publish_file(workspace, destination, (exported / rel).read_bytes())
                            files.append(destination)
                    job.metadata['artifacts'] = files
                except Exception as exc:
                    job.status = 'failed'
                    state['stderr'].extend(f'\n结果保存失败：{exc}'.encode())
        state['done'] = asyncio.create_task(finish())
        _jobs[identifier] = state
        return job

    async def job_status(self, job: SandboxJob | str) -> SandboxJobStatus:
        key = job if isinstance(job, str) else job.sandbox_job_id
        state = _jobs.get(key)
        if not state:
            return SandboxJobStatus(key, '', '', 'failed', error='本地进程已中断，不能跨重启恢复进程')
        value = state['job']
        return SandboxJobStatus(key, '', '', value.status, exit_code=state['exit_code'],
                                output=(state['stdout']+state['stderr']).decode(errors='replace'), metadata=value.metadata)

    async def job_output(self, job: SandboxJob | str, lines: int = 200) -> str:
        return '\n'.join((await self.job_status(job)).output.splitlines()[-max(1, lines):])

    async def terminate_job(self, job: SandboxJob | str) -> SandboxJobStatus:
        key = job if isinstance(job, str) else job.sandbox_job_id
        state = _jobs.get(key)
        if state and not state['done'].done():
            state['job'].status = 'terminated'
            state['proc'].stdin.close()  # Supervisor kills and reaps sole child on EOF.
            await asyncio.wait_for(asyncio.shield(state['done']), 5)
        return await self.job_status(job)

    async def run_once(self, spec: SandboxSpec) -> SandboxResult:
        job = await self.start_job(spec)
        state = _jobs[job.sandbox_job_id]
        try:
            await asyncio.shield(state['done'])
        except asyncio.CancelledError:
            await self.terminate_job(job)
            raise
        return SandboxResult(job.status, state['exit_code'], state['stdout'].decode(errors='replace'),
                             state['stderr'].decode(errors='replace'), sandbox_job_id=job.sandbox_job_id, metadata=job.metadata)

    async def list_persistent_runtimes(self, *, tenant_id='', user_id='', workspace_id='') -> list[dict]:
        owner = dict(tenant_id=tenant_id, user_id=user_id, workspace_id=workspace_id)
        return [{'runtime_id':key, 'backend':'macos-seatbelt', 'active_jobs':int(not value['done'].done()),
                 'status':value['job'].status, **{field:value['job'].metadata[field] for field in owner}}
                for key,value in _jobs.items() if all(not wanted or value['job'].metadata.get(field)==wanted for field,wanted in owner.items())]

    async def manage_persistent_runtime(self, runtime_id, action, *, owner=None, force=False, purge_data=False):
        state = _jobs.get(runtime_id)
        if not state or not owner or not all(state['job'].metadata.get(k)==v for k,v in owner.items()):
            raise ValueError('本地执行不存在或不属于当前用户')
        if action not in {'stop', 'delete'}:
            raise ValueError('原生执行不支持持久容器或恢复进程')
        await self.terminate_job(runtime_id)
        if action == 'delete':
            shutil.rmtree(state['work'].parent)
            state['work'].parent.with_suffix('.json').unlink(missing_ok=True)
            del _jobs[runtime_id]
        return {'status':'stopped' if action=='stop' else 'deleted', 'runtime_id':runtime_id}

    async def cleanup_workspace(self, *, tenant_id, user_id, workspace_id, purge_data=False):
        owner = dict(tenant_id=tenant_id, user_id=user_id, workspace_id=workspace_id)
        if not all(owner.values()):
            raise ValueError('本地执行清理缺少工作区身份')
        for key,state in list(_jobs.items()):
            if all(state['job'].metadata.get(k)==v for k,v in owner.items()):
                await self.terminate_job(key)
        if purge_data:
            directory = Path(os.environ['TCODE_WORKDIR']).resolve() / '.tcode/native-jobs'
            for manifest in directory.glob('native-*.json'):
                if len(manifest.stem) != 39 or any(c not in '0123456789abcdef' for c in manifest.stem[7:]):
                    continue
                if json.loads(manifest.read_text()) == owner:
                    root = manifest.with_suffix('')
                    if root.exists(): shutil.rmtree(root)
                    manifest.unlink()
                    _jobs.pop(manifest.stem, None)
        return {'status':'cleaned', **owner, 'runtime_data_purged':bool(purge_data)}

    async def restore_workspace(self, *, tenant_id, user_id, workspace_id):
        if not all((tenant_id, user_id, workspace_id)):
            raise ValueError('工作区身份不能为空')
        return {'status':'active', 'tenant_id':tenant_id, 'user_id':user_id, 'workspace_id':workspace_id}


async def stop_native_jobs() -> None:
    runtime = MacOSNativeRuntime()
    await asyncio.gather(*(runtime.terminate_job(key) for key, state in list(_jobs.items()) if not state['done'].done()))
