"""Exercise the custom MCP upload boundary without external services."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tcode_agent.mcp import artifact_boundary as boundary, custom_targets as custom


@pytest.fixture
def artifact_client(monkeypatch):
    async def post(url, **kwargs):
        result = {"artifact_id": "art_test"} if url.endswith("/artifacts") else {"bundle_id": "bundle_test"}
        return httpx.Response(200, json=result, request=httpx.Request("POST", url))

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.side_effect = post

    async def put(url, **kwargs):
        stream = kwargs["files"]["file"][1]
        while stream.read(64 * 1024):
            pass
        return httpx.Response(200, request=httpx.Request("PUT", url))

    client.put.side_effect = put
    monkeypatch.setattr(boundary.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("tcode_agent.artifact_transport._artifact_endpoint",
                        lambda *args, **kwargs: ("", "https://artifact.invalid", {}))
    return client


def upload_context(tmp_path, fields, progress):
    # Run the real POSIX descriptor path. Windows handle checks are covered by SDK tests.
    if os.name == "nt":
        pytest.importorskip("tcode_artifacts.windows_io")
    target = {"origin": custom.ORIGIN, "tools": [{"name": "analyze", "files": [
        {"pointer": f"/{name}", "kind": kind, "transport": "artifact"} for name, kind in fields
    ]}]}
    return SimpleNamespace(
        extra={"workdir": str(tmp_path), "workspace_root": str(tmp_path)},
        ask=AsyncMock(return_value=True), report_progress=progress,
        _custom_mcp_targets={"plugin-test": target},
    )


@pytest.mark.parametrize("replace", [True, False])
def test_upload_uses_the_files_own_grant_not_its_ancestors(tmp_path, artifact_client, replace):
    root = tmp_path / "inputs"
    source = root / "nested" / "input.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"approved")

    async def progress(event):
        if replace and event["message"].endswith(source.name):
            source.write_bytes(b"different content")

    # The directory grant is first, but the file field is uploaded first.
    args = {"single": str(source), "tree": str(root)}
    ctx = upload_context(tmp_path, [("tree", "directory"), ("single", "file")], progress)

    async def run():
        assert await custom.authorize_inputs(ctx, "plugin-test", "analyze", args)
        return await boundary.prepare_mcp_artifact_arguments("plugin-test", "analyze", args, ctx)

    if replace:
        with pytest.raises(boundary.ArtifactBoundaryError, match="已变化|approved input changed"):
            asyncio.run(run())
        artifact_client.post.assert_not_awaited()
    else:
        prepared, _ = asyncio.run(run())
        assert prepared["single"]["kind"] == "artifact"
        assert prepared["tree"]["kind"] == "artifact_bundle"
        artifact_client.put.assert_awaited_once()


@pytest.mark.parametrize("when", ["before_create", "during_create", "unchanged"])
def test_empty_bundle_checks_original_directory(tmp_path, artifact_client, when):
    root = tmp_path / "empty"
    root.mkdir()

    def replace():
        root.rename(tmp_path / "original")
        root.mkdir()

    async def progress(_event):
        if when == "before_create":
            replace()

    if when == "during_create":
        post = artifact_client.post.side_effect

        async def replace_during_create(url, **kwargs):
            if url.endswith("/bundles"):
                replace()
            return await post(url, **kwargs)

        artifact_client.post.side_effect = replace_during_create

    ctx = upload_context(tmp_path, [("tree", "directory")], progress)
    args = {"tree": str(root)}

    async def run():
        assert await custom.authorize_inputs(ctx, "plugin-test", "analyze", args)
        return await boundary.prepare_mcp_artifact_arguments("plugin-test", "analyze", args, ctx)

    if when == "unchanged":
        prepared, _ = asyncio.run(run())
        assert prepared["tree"]["bundle_id"] == "bundle_test"
        assert artifact_client.post.await_args.kwargs["json"] == {"expected_files": 0}
    else:
        with pytest.raises(ValueError, match="已变化"):
            asyncio.run(run())
        assert artifact_client.post.await_count == int(when == "during_create")
    artifact_client.put.assert_not_awaited()


def test_all_custom_fields_have_correct_types_before_first_upload(tmp_path, artifact_client):
    source = tmp_path / "input.txt"
    source.write_bytes(b"ok")
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    args = {"first": str(source), "second": str(directory)}
    ctx = upload_context(tmp_path, [("first", "file"), ("second", "file")], AsyncMock())

    async def run():
        assert await custom.authorize_inputs(ctx, "plugin-test", "analyze", args)
        return await boundary.prepare_mcp_artifact_arguments("plugin-test", "analyze", args, ctx)

    with pytest.raises((boundary.ArtifactBoundaryError, ValueError), match="目录|Windows 输入文件"):
        asyncio.run(run())
    artifact_client.post.assert_not_awaited()


@pytest.mark.parametrize("replacement", [b"X", b"X" * 64])
def test_nested_replacement_after_enumeration_is_not_uploaded(tmp_path, artifact_client, monkeypatch, replacement):
    tree = tmp_path / "tree"
    nested = tree / "branch" / "nested"
    nested.mkdir(parents=True)
    (nested / "input.txt").write_bytes(b"B")
    monkeypatch.setenv("TCODE_PLUGIN_UPLOAD_MAX_BYTES", "16")
    root_identity = tree.stat().st_mtime_ns
    post = artifact_client.post.side_effect

    async def replace_after_scan(url, **kwargs):
        if url.endswith("/bundles"):
            nested.rename(tmp_path / "original")
            nested.mkdir()
            (nested / "input.txt").write_bytes(replacement)
            assert tree.stat().st_mtime_ns == root_identity
        return await post(url, **kwargs)

    artifact_client.post.side_effect = replace_after_scan
    ctx = upload_context(tmp_path, [("tree", "directory")], AsyncMock())
    args = {"tree": str(tree)}

    async def run():
        assert await custom.authorize_inputs(ctx, "plugin-test", "analyze", args)
        return await boundary.prepare_mcp_artifact_arguments("plugin-test", "analyze", args, ctx)

    with pytest.raises(boundary.ArtifactBoundaryError, match="目录成员在检查后发生变化"):
        asyncio.run(run())
    artifact_client.put.assert_not_awaited()
    assert artifact_client.post.await_count == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows handles deny writes while the file is open")
def test_directory_member_cannot_send_more_than_its_planned_size(tmp_path, artifact_client):
    tree = tmp_path / "tree"
    tree.mkdir()
    source = tree / "input.txt"
    source.write_bytes(b"AB")
    sent = []

    async def grow_while_streaming(_url, **kwargs):
        body = kwargs["files"]["file"][1]
        sent.append(body.read(1))
        with source.open("ab") as stream:
            stream.write(b"unexpected growth")
        sent.append(body.read(64 * 1024))

    artifact_client.put.side_effect = grow_while_streaming
    ctx = upload_context(tmp_path, [("tree", "directory")], AsyncMock())
    args = {"tree": str(tree)}

    async def run():
        assert await custom.authorize_inputs(ctx, "plugin-test", "analyze", args)
        return await boundary.prepare_mcp_artifact_arguments("plugin-test", "analyze", args, ctx)

    with pytest.raises(boundary.ArtifactBoundaryError, match="目录成员大小在上传期间发生变化"):
        asyncio.run(run())
    assert sent == [b"A"]
    assert artifact_client.post.await_count == 1  # Never finalize the incomplete bundle.


def test_unchanged_directory_streams_valid_multipart(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "input.txt").write_bytes(b"content-from-file")
    async_client = httpx.AsyncClient
    requests = []

    async def serve(request):
        payload = await request.aread()
        requests.append(request.method)
        if request.method == "PUT":
            assert int(request.headers["content-length"]) == len(payload)
            assert b'filename="input.txt"' in payload
            assert b"\r\n\r\ncontent-from-file\r\n" in payload
        return httpx.Response(200, json={"bundle_id": "bundle_test"})

    monkeypatch.setattr(boundary.httpx, "AsyncClient",
                        lambda **kwargs: async_client(transport=httpx.MockTransport(serve), **kwargs))
    monkeypatch.setattr("tcode_agent.artifact_transport._artifact_endpoint",
                        lambda *args, **kwargs: ("", "https://artifact.invalid", {}))
    ctx = upload_context(tmp_path, [("tree", "directory")], AsyncMock())
    args = {"tree": str(tree)}

    async def run():
        assert await custom.authorize_inputs(ctx, "plugin-test", "analyze", args)
        return await boundary.prepare_mcp_artifact_arguments("plugin-test", "analyze", args, ctx)

    prepared, _ = asyncio.run(run())
    assert prepared["tree"]["bundle_id"] == "bundle_test"
    assert requests == ["POST", "PUT", "POST"]
