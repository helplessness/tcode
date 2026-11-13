"""Windows path policy is tested on every OS, independently of the native API."""
import pytest

from tcode_artifacts.windows_paths import absolute_path, bundle_paths, component


@pytest.mark.parametrize('name', [
    '.', '..', '', 'a:b', 'nul', 'CON.txt', 'LPT1.out', 'COM¹.txt',
    'CONIN$', 'CONOUT$', 'a.', 'a ', 'a\x00b', 'a\nb', 'a/b', 'a\\b',
    'a?b', 'a*b', 'a"b', 'a|b', 'a<b', 'SHORT~1', 'x' * 256,
])
def test_unsafe_components(name):
    with pytest.raises(ValueError):
        component(name)


@pytest.mark.parametrize('path', [
    'relative/file', 'C:relative', '/root/file', '//server/share/file',
    '\\\\?\\C:\\file', '\\\\.\\pipe\\file', 'C:/', 'C:/a/../b',
    'C:/a/./b', 'C:/a//b', 'C:/a/', 'C:/a/CON.dat', 'C:/a/file:stream',
])
def test_unsafe_absolute_paths(path):
    with pytest.raises(ValueError):
        absolute_path(path)


@pytest.mark.parametrize('paths', [
    ['a', 'A'], ['x/a', 'X/b'], ['x', 'x/a'], ['x/a', 'x'],
    ['a', 'a'], ['safe/a:stream'], ['safe/NUL.txt'], ['safe/a.'],
])
def test_manifest_aliases_and_prefix_conflicts(paths):
    with pytest.raises(ValueError):
        bundle_paths(paths)


def test_valid_names_and_multilingual_paths():
    assert str(absolute_path('C:/任务 1/input.dat')) == 'C:\\任务 1\\input.dat'
    bundle_paths(['中文/a.dat', '中文/b.dat', 'other/NUL_safe', 'x/COM10.txt'])
    assert component('résultat🧪.dat') == 'résultat🧪.dat'


def test_windows_manifest_preflight_happens_before_disk_creation(tmp_path, monkeypatch):
    from tcode_artifacts import client
    io = client.ArtifactIO('https://artifact.invalid')
    monkeypatch.setattr(client, '_WINDOWS', True)
    monkeypatch.setattr(io, '_request', lambda *args, **kwargs: {
        'state': 'ready', 'files': [
            {'relative_path': name, 'artifact_id': 'art_x', 'size': 1}
            for name in ['Input.dat', 'input.dat']
        ]})
    with pytest.raises(ValueError, match='collid'):
        io.materialize({'kind': 'artifact_bundle', 'bundle_id': 'b'}, tmp_path / 'bundle')
    assert not (tmp_path / 'bundle').exists()
