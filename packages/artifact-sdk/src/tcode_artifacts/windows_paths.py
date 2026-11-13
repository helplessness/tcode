"""Windows lexical admission, importable on every platform for contract tests."""
import re
from pathlib import PureWindowsPath


def component(name):
    if (not name or name in {'.', '..'} or name[-1] in ' .'
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
            or '~' in name):
        raise ValueError('unsafe Windows path component')
    stem = name.split('.', 1)[0].rstrip(' ').upper()
    if stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} or re.fullmatch(r'(COM|LPT)[1-9¹²³]', stem):
        raise ValueError('reserved Windows path component')
    # Reject lone surrogates, overlong names and DOS short-name aliases.
    if len(name.encode('utf-16-le')) > 510:
        raise ValueError('Windows path component is too long')
    return name


def absolute_path(value):
    raw = str(value)
    path = PureWindowsPath(raw)
    if not path.is_absolute() or not re.fullmatch(r'[A-Za-z]:', path.drive):
        raise ValueError('Windows SDK requires an absolute local drive path; UNC/device paths are not supported')
    # Validate before any normalization can discard traversal components.
    tail = raw[3:].replace('\\', '/')
    if not tail or any(part in {'', '.', '..'} for part in tail.split('/')):
        raise ValueError('unsafe Windows path')
    for part in path.parts[1:]:
        component(part)
    return path


def bundle_paths(paths):
    """Reject aliases and file/directory prefix conflicts before any disk writes."""
    files, directories, spellings = set(), set(), {}
    for value in paths:
        parts = value.split('/')
        for part in parts:
            component(part)
        prefixes = ['/'.join(parts[:index]) for index in range(1, len(parts) + 1)]
        keys = [prefix.casefold() for prefix in prefixes]
        for prefix, key in zip(prefixes, keys):
            if key in spellings and spellings[key] != prefix:
                raise ValueError('case-colliding Windows bundle paths')
            spellings[key] = prefix
        if keys[-1] in files or keys[-1] in directories or any(key in files for key in keys[:-1]):
            raise ValueError('conflicting Windows bundle paths')
        files.add(keys[-1])
        directories.update(keys[:-1])
