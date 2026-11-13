"""Optional example acceptance: requires the SDK's example extra, no external MCP."""
import importlib.util
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from tcode_artifacts import ArtifactIO


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_real_mcp_discovery_and_file_echo(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    contract = load('sdk_contract_fixture', root / 'tests/test_contract.py')
    server = load('sdk_minimal_mcp', root / 'examples/mcp_server.py')
    store = contract.Store()
    io = ArtifactIO('https://artifact.invalid', transport=httpx.MockTransport(store.handle))
    monkeypatch.setattr(server, 'ArtifactIO', lambda: io)
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        tool = next(tool for tool in tools if tool.name == 'echo_file')
        assert tool.inputSchema['properties']['input_file']['type'] == 'object'
        result = await client.call_tool('echo_file', {'input_file': {'kind': 'artifact', 'artifact_id': 'art_input'}})
        assert result.structured_content['status'] == 'completed'
        ref = result.structured_content['output_bundle']
    received = io.materialize(ref, tmp_path / 'received')
    assert (received / 'echo.dat').read_bytes() == store.content['art_input']
    assert len(store.bundles) == 1


@pytest.mark.anyio
async def test_remote_job_file_result_survives_server_reload(tmp_path, monkeypatch):
    package = Path(__file__).resolve().parents[1]
    example = package.parents[1] / 'examples' / 'third_party_mcp'
    if not example.is_dir():
        pytest.skip('remote-job example belongs to the source repository, not the standalone wheel')
    contract = load('remote_sdk_contract_fixture', package / 'tests/test_contract.py')
    store = contract.Store()
    io = ArtifactIO('https://artifact.invalid', transport=httpx.MockTransport(store.handle))
    monkeypatch.syspath_prepend(str(example))
    monkeypatch.setenv('EXAMPLE_DATA_ROOT', str(tmp_path / 'jobs'))
    monkeypatch.delenv('EXAMPLE_MCP_TOKEN', raising=False)
    server = load('sdk_remote_example', example / 'remote_server.py')
    monkeypatch.setattr(server, 'ArtifactIO', lambda: io)
    arguments = {'input_file': {'kind': 'artifact', 'artifact_id': 'art_input'}, 'delay_seconds': 0}
    async with Client(server.mcp) as client:
        first = await client.call_tool('submit_prediction', {'request_id': 'sdk-file-test', 'tool': 'predict', 'arguments': arguments})
        again = await client.call_tool('submit_prediction', {'request_id': 'sdk-file-test', 'tool': 'predict', 'arguments': arguments})
        assert first.structured_content['job_id'] == again.structured_content['job_id']
        identifier = first.structured_content['job_id']
        result = await client.call_tool('get_prediction_result', {'job_id': identifier})
        ref = result.structured_content['output_bundle']
    # A fresh server module reads the persisted reference, without a second upload.
    restarted = load('sdk_remote_example_reloaded', example / 'remote_server.py')
    monkeypatch.setattr(restarted, 'ArtifactIO', lambda: io)
    async with Client(restarted.mcp) as client:
        recovered = await client.call_tool('get_prediction_result', {'job_id': identifier})
        assert recovered.structured_content['output_bundle'] == ref
    received = io.materialize(ref, tmp_path / 'received')
    assert (received / 'inputs/input.dat').read_bytes() == store.content['art_input']
    assert '第三方计算完成' in (received / 'reports/summary.json').read_text(encoding='utf-8')
    assert len(store.bundles) == 1
