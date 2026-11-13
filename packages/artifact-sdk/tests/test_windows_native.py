"""Must run on actual Windows: python -m pytest packages/artifact-sdk/tests -q.

All filesystem mutations are confined to pytest temporary directories. No MCP,
credentials, GPU, shell installation or network services are required here.
"""
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='requires native Windows NTFS APIs')


def test_windows_stream_and_failed_download_cleanup(tmp_path):
    from tcode_artifacts import windows_io as fs
    path = tmp_path / '目录' / '输入.dat'
    with pytest.raises(RuntimeError, match='interrupted'):
        with fs.new_file(path) as stream:
            stream.write(b'partial')
            raise RuntimeError('interrupted')
    assert not path.exists()
    with fs.new_file(path) as stream:
        stream.write(b'complete')
    with fs.open_file(path) as stream:
        assert stream.read() == b'complete'
    with pytest.raises(FileExistsError):
        with fs.new_file(path):
            pytest.fail('must not overwrite')
    assert path.read_bytes() == b'complete'


def test_windows_parent_and_final_handle_cannot_be_replaced(tmp_path):
    from tcode_artifacts import windows_io as fs
    parent = tmp_path / 'parent'
    parent.mkdir()
    path = parent / 'output'
    with fs.new_file(path) as stream:
        stream.write(b'owned')
        with pytest.raises(OSError):
            parent.rename(tmp_path / 'moved')
        with pytest.raises(OSError):
            path.unlink()
    assert path.read_bytes() == b'owned'
    parent.rename(tmp_path / 'moved')  # handles were released after success


@pytest.mark.parametrize('directory', [False, True])
def test_windows_input_grant_is_checked_on_the_held_handle(tmp_path, directory):
    from tcode_artifacts import windows_io as fs

    tree = tmp_path / 'tree'
    tree.mkdir()
    source = tree / 'input'
    source.write_bytes(b'approved')
    approved = tree if directory else source
    grant = (approved, fs.input_identity(approved, directory=directory), directory)
    with fs.open_file(source, grant=grant) as stream:
        assert stream.read() == b'approved'
        with pytest.raises(OSError):
            approved.rename(tmp_path / 'moved')

    approved.rename(tmp_path / 'moved')
    tree.mkdir(exist_ok=True)
    source.write_bytes(b'replaced')
    with pytest.raises(ValueError, match='approved input changed'):
        with fs.open_file(source, grant=grant):
            pytest.fail('must not return replacement bytes')


def test_windows_junction_rejected_in_parent_and_enumeration(tmp_path):
    from tcode_artifacts import windows_io as fs
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_bytes(b'not admitted')
    tree = tmp_path / 'tree'
    tree.mkdir()
    link = tree / 'junction'
    # Directory junction creation does not require symlink developer privileges.
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0, 'could not create the test junction'
    try:
        with pytest.raises((OSError, ValueError)):
            with fs.new_file(link / 'escape'):
                pytest.fail('followed junction')
        with pytest.raises((OSError, ValueError)):
            with fs.open_file(link / 'secret'):
                pytest.fail('read outside through junction')
        with pytest.raises((OSError, ValueError)):
            list(fs.scan_files(tree, max_files=100))
        assert not (outside / 'escape').exists()
    finally:
        link.rmdir()  # remove the test junction itself, never its target


def test_windows_hardlinked_source_is_rejected(tmp_path):
    from tcode_artifacts import windows_io as fs
    original = tmp_path / 'original'
    original.write_bytes(b'keep')
    alias = tmp_path / 'alias'
    os.link(original, alias)
    with pytest.raises(ValueError, match='hard-linked'):
        with fs.open_file(alias):
            pytest.fail('read a hard-linked source')
    assert original.read_bytes() == b'keep'


def test_windows_directory_enumeration_multiple_buffers(tmp_path):
    from tcode_artifacts import windows_io as fs
    tree = tmp_path / 'output'
    tree.mkdir()
    for i in range(600):
        (tree / (f'{i:04d}-' + 'x' * 120)).write_bytes(b'x')
    entries = list(fs.scan_files(tree, max_files=600))
    assert len(entries) == 600
    assert len({name for name, _ in entries}) == 600
    assert all(info.st_size == 1 for _, info in entries)


def test_windows_new_bundle_root_remains_usable_while_pinned(tmp_path):
    from tcode_artifacts import windows_io as fs
    tree = tmp_path / 'bundle'
    with fs.new_directory(tree):
        with fs.new_file(tree / 'nested' / 'data') as stream:
            stream.write(b'ok')
        with pytest.raises(OSError):
            tree.rename(tmp_path / 'other')
    assert (tree / 'nested' / 'data').read_bytes() == b'ok'
