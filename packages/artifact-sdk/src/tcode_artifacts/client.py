"""Use the existing Artifact HTTP API, with locally configured service routing."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

_WINDOWS = os.name == 'nt'


def _local_path(path):
    if _WINDOWS:
        from .windows_paths import absolute_path
        return Path(absolute_path(path))
    return Path(os.path.abspath(path))


@contextmanager
def _new_file(path):
    if _WINDOWS:
        from .windows_io import new_file
        with new_file(path) as stream:
            yield stream
        return
    with _parent(path, create=True) as (parent, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                yield stream
        except BaseException:
            os.unlink(name, dir_fd=parent)
            raise


@contextmanager
def _open_file(path):
    if _WINDOWS:
        from .windows_io import open_file
        with open_file(path) as stream:
            yield stream
        return
    with _parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            yield stream


@contextmanager
def _new_directory(path):
    if _WINDOWS:
        from .windows_io import new_directory
        with new_directory(path):
            yield
        return
    with _parent(path, create=True) as (parent, name):
        os.mkdir(name, 0o700, dir_fd=parent)
    yield


def _scan_files(root, *, max_files):
    if _WINDOWS:
        from .windows_io import scan_files
        yield from scan_files(root, max_files=max_files)
        return
    with _parent(root) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            for current, directories, names, directory_fd in os.fwalk('.', dir_fd=descriptor, follow_symlinks=False):
                for entry in directories + names:
                    info = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise ValueError('symlinks and special files are not allowed')
                    if stat.S_ISREG(info.st_mode):
                        yield (Path(current) / entry).as_posix(), info
        finally:
            os.close(descriptor)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise ValueError('invalid artifact, bundle or service ID')
    return value


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('unsafe bundle path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')) or ':' in path.parts[0]:
        raise ValueError('unsafe bundle path')
    return path


@contextmanager
def _parent(path, *, create=False):
    """Walk anchored descriptors: never follow parent or final symlinks."""
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise RuntimeError('secure file I/O requires POSIX dir_fd and O_NOFOLLOW')
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class ArtifactIO:
    """Synchronous, bounded file I/O. References never select arbitrary URLs."""

    def __init__(self, base_url=None, *, token=None, service_id=None, services=None,
                 max_bytes=None, max_files=None, timeout=300, transport=None):
        self._explicit_url = base_url is not None
        self.url = base_url if base_url is not None else os.environ.get('TMCP_ARTIFACT_SERVICE_URL', '')
        self.token = token if token is not None else os.environ.get('TMCP_ARTIFACT_API_TOKEN', '')
        self.service_id = service_id if service_id is not None else os.environ.get('TMCP_ARTIFACT_SERVICE_ID', '')
        self.services = services if services is not None else json.loads(os.environ.get('TMCP_ARTIFACT_SERVICES', '{}'))
        if not isinstance(self.services, dict):
            raise ValueError('services must be a mapping of trusted service IDs')
        self.limit = int(max_bytes if max_bytes is not None else os.environ.get('EXAMPLE_MAX_BYTES', 500 * 1024 * 1024))
        self.max_files = int(max_files if max_files is not None else os.environ.get('EXAMPLE_MAX_FILES', 10000))
        self.timeout, self.transport = timeout, transport
        if self.limit < 1 or self.max_files < 1 or timeout <= 0:
            raise ValueError('limits and timeout must be positive')
        self._endpoint(self.service_id)

    def _endpoint(self, service_id):
        if service_id:
            _identifier(service_id)
        url, token = self.url, self.token
        if service_id in self.services:
            config = self.services[service_id]
            if not isinstance(config, dict):
                raise ValueError('service configuration must be an object')
            url, name = config.get('url', ''), config.get('token_env', '')
            if not isinstance(name, str) or (name and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)):
                raise ValueError('invalid token_env')
            token = os.environ.get(name, '') if name else ''
            if name and not token:
                raise ValueError('Artifact service credential is not configured')
            if self._explicit_url and service_id == self.service_id and self.url.rstrip('/') != str(url).rstrip('/'):
                raise ValueError('default Artifact service identity conflicts with URL')
        elif service_id and service_id != self.service_id:
            raise ValueError('unknown Artifact service_id')
        if not isinstance(url, str):
            raise ValueError('invalid Artifact service URL')
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid Artifact service URL')
        parsed.port  # Validate port without exposing credentials in an exception.
        return url.rstrip('/'), {'X-Artifact-Token': token} if token else {}

    def _client(self, service_id):
        url, headers = self._endpoint(service_id)
        return httpx.Client(base_url=url + '/', headers=headers, timeout=self.timeout,
                            follow_redirects=False, transport=self.transport)

    def _request(self, method, path, *, service_id, **kwargs):
        with self._client(service_id) as client:
            response = client.request(method, path.lstrip('/'), **kwargs)
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Artifact response must be an object')
        actual = (result.get('ref') or {}).get('service_id')
        if actual and actual != service_id:
            raise ValueError('Artifact service identity mismatch')
        return result

    def request(self, method, path, **kwargs):
        """Compatibility entry for the original single-service example."""
        return self._request(method, path, service_id=self.service_id, **kwargs)

    def download_file(self, identifier, destination, *, service_id=None, max_bytes=None, expected_metadata=None):
        selected = self.service_id if service_id is None else service_id
        key = _identifier(identifier)
        meta = self._request('GET', f'/api/v1/artifacts/{key}', service_id=selected)
        limit = self.limit if max_bytes is None else min(self.limit, max_bytes)
        size, digest = meta.get('size'), meta.get('sha256')
        if type(size) is not int or size < 0 or size > limit or not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{64}', digest):
            raise ValueError('invalid artifact metadata or size limit exceeded')
        if expected_metadata is not None and (size != expected_metadata.get('size') or digest.lower() != str(expected_metadata.get('sha256', '')).lower()):
            raise ValueError('bundle and artifact metadata integrity mismatch')
        target = _local_path(destination)
        with _new_file(target) as output, self._client(selected) as client:
            count, checksum = 0, hashlib.sha256()
            with client.stream('GET', f'api/v1/artifacts/{key}/content') as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    count += len(chunk)
                    if count > limit or count > size:
                        raise ValueError('artifact size limit exceeded')
                    output.write(chunk)
                    checksum.update(chunk)
            if count != size or checksum.hexdigest() != digest.lower():
                raise ValueError('artifact integrity mismatch')
        return target

    def materialize(self, ref, destination):
        if not isinstance(ref, dict):
            raise ValueError('expected ArtifactRef or BundleRef')
        if not isinstance(ref.get('service_id', ''), str):
            raise ValueError('service_id must be a string')
        selected = ref.get('service_id', '') or self.service_id
        self._endpoint(selected)
        if ref.get('kind') == 'artifact':
            return self.download_file(ref.get('artifact_id'), destination, service_id=selected)
        if ref.get('kind') != 'artifact_bundle':
            raise ValueError('expected ArtifactRef or BundleRef')
        key = _identifier(ref.get('bundle_id'))
        bundle = self._request('GET', f'/api/v1/bundles/{key}', service_id=selected)
        files = bundle.get('files')
        if bundle.get('state') != 'ready' or not isinstance(files, list) or len(files) > self.max_files:
            raise ValueError('bundle not ready or too many files')
        paths, total = set(), 0
        for item in files:
            path = _relative(item['relative_path'])
            if str(path) in paths:
                raise ValueError('duplicate bundle path')
            paths.add(str(path))
            _identifier(item.get('artifact_id'))
            if type(item.get('size')) is not int or item['size'] < 0:
                raise ValueError('invalid bundle size')
            total += item['size']
        if total > self.limit:
            raise ValueError('bundle exceeds total size limit')
        if _WINDOWS:
            from .windows_paths import bundle_paths
            bundle_paths([item['relative_path'] for item in files])
        root = _local_path(destination)
        with _new_directory(root):
            downloaded = 0
            for item in files:
                self.download_file(item['artifact_id'], root / str(_relative(item['relative_path'])),
                                   service_id=selected, max_bytes=self.limit - downloaded, expected_metadata=item)
                downloaded += item['size']  # actual streamed size has already been verified
        return root

    def _upload(self, path, method, endpoint, *, expected=None, **kwargs):
        path = _local_path(path)
        with _open_file(path) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > self.limit or (expected and _identity(before) != expected):
                raise ValueError('upload source changed, not a regular file, or exceeds size limit')
            result = self._request(method, endpoint, service_id=self.service_id, files={'file': (path.name, stream)}, **kwargs)
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise ValueError('upload source changed during transfer')
            return result

    def _ref(self, kind, key, response):
        result = {'kind': kind, key: _identifier(response.get(key))}
        if self.service_id:
            result['service_id'] = self.service_id
        return result

    def upload_file(self, source, *, workspace_id='third-party'):
        return self._ref('artifact', 'artifact_id', self._upload(source, 'POST', '/api/v1/artifacts', data={'workspace_id': workspace_id}))

    def upload_directory(self, source, *, workspace_id='third-party'):
        root = _local_path(source)
        files, total = [], 0
        for relative, info in _scan_files(root, max_files=self.max_files):
            files.append((relative, _identity(info)))
            total += info.st_size
            if len(files) > self.max_files or total > self.limit:
                raise ValueError('output exceeds upload limit')
        if _WINDOWS:
            from .windows_paths import bundle_paths
            bundle_paths([relative for relative, _ in files])
        bundle = self.request('POST', '/api/v1/bundles', json={'name': root.name, 'workspace_id': workspace_id})
        key = _identifier(bundle.get('bundle_id'))
        for relative, expected in sorted(files):
            self._upload(root / relative, 'PUT', f'/api/v1/bundles/{key}/files', expected=expected, params={'relative_path': relative})
        completed = self.request('POST', f'/api/v1/bundles/{key}/complete', json={'expected_files': len(files)})
        if completed.get('state') != 'ready':
            raise ValueError('output bundle was not finalized')
        return self._ref('artifact_bundle', 'bundle_id', bundle)
