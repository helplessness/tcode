"""Standalone contracts against the existing Artifact HTTP routes; no TCode imports."""
import hashlib
import importlib.util
import json
import sys
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest

from tcode_artifacts import ArtifactIO


class Store:
    def __init__(self):
        self.content = {'art_input': b'synthetic-input\x00\xff'}
        self.bundles = {}
        self.calls = []

    def meta(self, key):
        content = self.content[key]
        return {'artifact_id': key, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}

    def handle(self, request):
        self.calls.append(request)
        path, method = request.url.path, request.method
        if path.startswith('/api/v1/artifacts/') and method == 'GET':
            key = path.split('/')[4]
            return httpx.Response(200, content=self.content[key]) if path.endswith('/content') else httpx.Response(200, json=self.meta(key))
        if method == 'GET':
            return httpx.Response(200, json=self.bundles[path.split('/')[4]])
        if method == 'POST' and path == '/api/v1/bundles':
            key = f'bundle_{len(self.bundles)}'
            self.bundles[key] = {'bundle_id': key, 'state': 'draft', 'files': []}
            return httpx.Response(200, json=self.bundles[key])
        if path.endswith('/complete'):
            bundle = self.bundles[path.split('/')[4]]
            assert json.loads(request.read())['expected_files'] == len(bundle['files'])
            bundle['state'] = 'ready'
            return httpx.Response(200, json=bundle)
        message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\n\r\n' + request.read())
        data = next(part.get_payload(decode=True) for part in message.iter_parts() if part.get_param('name', header='content-disposition') == 'file')
        key = f'art_{len(self.content)}'
        self.content[key] = data
        if method == 'PUT':
            self.bundles[path.split('/')[4]]['files'].append({**self.meta(key), 'relative_path': request.url.params['relative_path']})
        return httpx.Response(200, json=self.meta(key))


@pytest.fixture
def store():
    return Store()


def client(store, **kwargs):
    return ArtifactIO('https://artifact.invalid', transport=httpx.MockTransport(store.handle), **kwargs)


def test_file_and_bundle_roundtrip_and_new_client_reuses_references(store, tmp_path):
    io = client(store)
    original = io.materialize({'kind': 'artifact', 'artifact_id': 'art_input'}, tmp_path / 'input')
    assert original.read_bytes() == store.content['art_input']
    uploaded = io.upload_file(original)
    assert uploaded['kind'] == 'artifact'
    output = tmp_path / 'output'
    (output / 'nested').mkdir(parents=True)
    (output / 'nested' / 'result.dat').write_bytes(original.read_bytes())
    ref = io.upload_directory(output)
    # Fresh client: SDK keeps no queue or job state and does not resubmit work.
    restored = client(store).materialize(ref, tmp_path / 'restored')
    assert (restored / 'nested/result.dat').read_bytes() == original.read_bytes()
    assert len(store.bundles) == 1
    assert sum(r.url.path.endswith('/complete') for r in store.calls) == 1


def test_real_loopback_http_roundtrip(store, tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def serve(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            response = store.handle(httpx.Request(self.command, 'http://localhost' + self.path, headers=dict(self.headers), content=body))
            self.send_response(response.status_code)
            self.send_header('Content-Type', response.headers.get('content-type', 'application/octet-stream'))
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = do_POST = do_PUT = serve

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        source = tmp_path / 'source'
        source.mkdir()
        (source / 'bytes.dat').write_bytes(b'local HTTP contract')
        ref = ArtifactIO(url).upload_directory(source)
        received = ArtifactIO(url).materialize(ref, tmp_path / 'received')
        assert (received / 'bytes.dat').read_bytes() == b'local HTTP contract'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_service_routing_uses_only_configured_credentials(store, tmp_path, monkeypatch):
    monkeypatch.setenv('B_TOKEN', 'beta-test-token')
    io = client(store, token='alpha-test-token', service_id='alpha', services={
        'beta': {'url': 'https://beta.invalid', 'token_env': 'B_TOKEN'}})
    io.materialize({'kind': 'artifact', 'artifact_id': 'art_input', 'service_id': 'beta', 'url': 'https://untrusted.invalid'}, tmp_path / 'beta')
    assert all(r.url.host == 'beta.invalid' and r.headers['X-Artifact-Token'] == 'beta-test-token' for r in store.calls)
    count = len(store.calls)
    with pytest.raises(ValueError, match='unknown'):
        io.materialize({'kind': 'artifact', 'artifact_id': 'art_input', 'service_id': 'unknown'}, tmp_path / 'unknown')
    assert len(store.calls) == count
    source = tmp_path / 'source'
    source.write_text('default output')
    assert io.upload_file(source)['service_id'] == 'alpha'
    assert store.calls[-1].headers['X-Artifact-Token'] == 'alpha-test-token'


def test_redirect_not_followed_and_no_automatic_retry(tmp_path):
    calls = []
    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={'location': 'https://untrusted.invalid'})
    io = ArtifactIO('https://artifact.invalid', token='test-token', transport=httpx.MockTransport(redirect))
    with pytest.raises(httpx.HTTPStatusError):
        io.materialize({'kind': 'artifact', 'artifact_id': 'art_input'}, tmp_path / 'input')
    assert len(calls) == 1


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'a/../../escape', 'a\\b', '', '.', 'a//b', 'a/./b', 'C:/escape'])
def test_bundle_paths_fail_before_writing_or_downloading(store, tmp_path, path):
    store.bundles['bundle_bad'] = {'state': 'ready', 'files': [{**store.meta('art_input'), 'relative_path': path}]}
    with pytest.raises(ValueError, match='path'):
        client(store).materialize({'kind': 'artifact_bundle', 'bundle_id': 'bundle_bad'}, tmp_path / 'bundle')
    assert not (tmp_path / 'bundle').exists()
    assert len(store.calls) == 1


@pytest.mark.parametrize('case', ['hash', 'size', 'total', 'files', 'not_ready', 'duplicate', 'metadata_drift'])
def test_integrity_and_quota_failures_never_report_success(store, tmp_path, case):
    io = client(store)
    meta = store.meta('art_input')
    files = [{**meta, 'relative_path': 'input'}]
    if case == 'hash':
        original_meta = store.meta
        store.meta = lambda key: {**original_meta(key), 'sha256': '0' * 64}
    elif case == 'size':
        original_meta = store.meta
        store.meta = lambda key: {**original_meta(key), 'size': 1}
    elif case == 'total':
        io.limit = 1
    elif case == 'files':
        files += [{**meta, 'relative_path': 'second'}]
        io.max_files = 1
    elif case == 'duplicate':
        files += files
    elif case == 'metadata_drift':
        files[0]['sha256'] = '0' * 64
    store.bundles['bundle_input'] = {'state': 'draft' if case == 'not_ready' else 'ready', 'files': files}
    with pytest.raises(ValueError):
        io.materialize({'kind': 'artifact_bundle', 'bundle_id': 'bundle_input'}, tmp_path / 'output')
    assert not (tmp_path / 'output/input').exists()


def test_existing_files_and_symlinks_not_overwritten_or_read(store, tmp_path):
    io = client(store)
    existing = tmp_path / 'keep'
    existing.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        io.materialize({'kind': 'artifact', 'artifact_id': 'art_input'}, existing)
    parent = tmp_path / 'link'
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        io.materialize({'kind': 'artifact', 'artifact_id': 'art_input'}, parent / 'unsafe')
    source = tmp_path / 'output'
    source.mkdir()
    (source / 'link').symlink_to(existing)
    with pytest.raises(ValueError, match='symlink'):
        io.upload_directory(source)
    assert existing.read_bytes() == b'keep' and not (tmp_path / 'unsafe').exists()
    assert not store.bundles


def test_upload_preflight_limits_and_source_swap(store, tmp_path):
    source = tmp_path / 'output'
    source.mkdir()
    file = source / 'input'
    file.write_bytes(b'test')
    with pytest.raises(ValueError, match='limit'):
        client(store, max_bytes=1).upload_directory(source)
    assert not store.calls
    io = client(store)
    request = io.request
    def swap(*args, **kwargs):
        result = request(*args, **kwargs)
        file.unlink()
        file.symlink_to(tmp_path / 'outside')
        return result
    io.request = swap
    with pytest.raises(OSError):
        io.upload_directory(source)
    assert not any(r.method == 'PUT' for r in store.calls)


def test_unfinalized_output_and_ambiguous_upload_fail_closed(store, tmp_path):
    source = tmp_path / 'output'
    source.mkdir()
    (source / 'data').write_bytes(b'output')
    original = store.handle
    def unfinished(request):
        if request.url.path.endswith('/complete'):
            return httpx.Response(200, json={'state': 'draft'})
        return original(request)
    io = ArtifactIO('https://artifact.invalid', transport=httpx.MockTransport(unfinished))
    with pytest.raises(ValueError, match='finalized'):
        io.upload_directory(source)
    calls = []
    def ambiguous(request):
        calls.append(request)
        raise httpx.ReadTimeout('upload outcome unknown')
    io = ArtifactIO('https://artifact.invalid', transport=httpx.MockTransport(ambiguous))
    with pytest.raises(httpx.ReadTimeout):
        io.upload_directory(source)
    assert len(calls) == 1


def test_old_example_import_is_the_same_sdk_class():
    root = Path(__file__).resolve().parents[3]
    if root.name == 'tcode':
        path = root / 'examples/third_party_mcp/artifact_io.py'
        spec = importlib.util.spec_from_file_location('old_example_artifact_io', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.ArtifactIO is ArtifactIO
    assert 'tcode_agent' not in sys.modules and 'tmcp' not in sys.modules
