"""Handle-relative Windows I/O, restricted to local NTFS; no pathname fallback.

Ancestors stay open without delete sharing. Every child is opened relative to
its parent with NtCreateFile + OBJ_DONT_REPARSE, never resolve/check/ordinary-open.
New objects receive a protected DACL for the current user and SYSTEM.
Requires Windows 10+, and is imported only by the Windows client branch.
"""
import ctypes as c
from ctypes import wintypes as w
from contextlib import contextmanager, ExitStack
import msvcrt
import os
import struct

from .windows_paths import absolute_path, component


class UnicodeString(c.Structure):
    _fields_ = [('Length', w.USHORT), ('MaximumLength', w.USHORT), ('Buffer', w.LPWSTR)]


class ObjectAttributes(c.Structure):
    _fields_ = [('Length', w.ULONG), ('RootDirectory', w.HANDLE),
                ('ObjectName', c.POINTER(UnicodeString)), ('Attributes', w.ULONG),
                ('SecurityDescriptor', w.LPVOID), ('SecurityQualityOfService', w.LPVOID)]


class IoStatus(c.Structure):
    _fields_ = [('Status', c.c_void_p), ('Information', c.c_size_t)]


class FileInfo(c.Structure):
    _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME),
                ('accessed', w.FILETIME), ('modified', w.FILETIME),
                ('volume', w.DWORD), ('size_high', w.DWORD), ('size_low', w.DWORD),
                ('links', w.DWORD), ('index_high', w.DWORD), ('index_low', w.DWORD)]


class FileBasicInfo(c.Structure):
    _fields_ = [('created', c.c_int64), ('accessed', c.c_int64),
                ('modified', c.c_int64), ('changed', c.c_int64), ('attributes', w.DWORD)]


kernel = c.WinDLL('kernel32', use_last_error=True)
nt = c.WinDLL('ntdll', use_last_error=True)
advapi = c.WinDLL('advapi32', use_last_error=True)


def _api(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


_close = _api(kernel, 'CloseHandle', [w.HANDLE], w.BOOL)
_create = _api(kernel, 'CreateFileW', [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
_info = _api(kernel, 'GetFileInformationByHandle', [w.HANDLE, c.POINTER(FileInfo)], w.BOOL)
_get_ex = _api(kernel, 'GetFileInformationByHandleEx', [w.HANDLE, c.c_int, w.LPVOID, w.DWORD], w.BOOL)
_set_ex = _api(kernel, 'SetFileInformationByHandle', [w.HANDLE, c.c_int, w.LPVOID, w.DWORD], w.BOOL)
_drive_type = _api(kernel, 'GetDriveTypeW', [w.LPCWSTR], w.UINT)
_volume = _api(kernel, 'GetVolumeInformationByHandleW', [w.HANDLE, w.LPWSTR, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPWSTR, w.DWORD], w.BOOL)
_ntcreate = _api(nt, 'NtCreateFile', [c.POINTER(w.HANDLE), w.DWORD, c.POINTER(ObjectAttributes), c.POINTER(IoStatus),
                                    w.LPVOID, w.ULONG, w.ULONG, w.ULONG, w.ULONG, w.LPVOID, w.ULONG], w.LONG)
_dos_error = _api(nt, 'RtlNtStatusToDosError', [w.LONG], w.ULONG)
_process = _api(kernel, 'GetCurrentProcess', [], w.HANDLE)
_free = _api(kernel, 'LocalFree', [w.LPVOID], w.LPVOID)
_open_token = _api(advapi, 'OpenProcessToken', [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], w.BOOL)
_token_info = _api(advapi, 'GetTokenInformation', [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)], w.BOOL)
_sid_string = _api(advapi, 'ConvertSidToStringSidW', [w.LPVOID, c.POINTER(w.LPWSTR)], w.BOOL)
_security = _api(advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', [w.LPCWSTR, w.DWORD, c.POINTER(w.LPVOID), w.LPVOID], w.BOOL)


def _check(ok):
    if not ok:
        raise c.WinError(c.get_last_error())


@contextmanager
def _owned(handle):
    try:
        yield handle
    finally:
        _close(handle)


@contextmanager
def _private_security():
    token = w.HANDLE()
    _check(_open_token(_process(), 0x0008, c.byref(token)))  # TOKEN_QUERY
    with _owned(token):
        length = w.DWORD()
        _token_info(token, 1, None, 0, c.byref(length))  # TokenUser
        if not length.value:
            raise c.WinError(c.get_last_error())
        data = c.create_string_buffer(length.value)
        _check(_token_info(token, 1, data, length, c.byref(length)))
        sid = c.cast(data, c.POINTER(w.LPVOID))[0]
        text = w.LPWSTR()
        _check(_sid_string(sid, c.byref(text)))
        try:
            sddl = f'D:P(A;;FA;;;{text.value})(A;;FA;;;SY)'
        finally:
            _free(c.cast(text, w.LPVOID))
    descriptor = w.LPVOID()
    _check(_security(sddl, 1, c.byref(descriptor), None))
    try:
        yield descriptor
    finally:
        _free(descriptor)


def _validate(handle, directory):
    info = FileInfo()
    _check(_info(handle, c.byref(info)))
    if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise ValueError('symlinks and Windows reparse points are not allowed')
    if bool(info.attributes & 0x10) != directory or (not directory and info.links != 1):
        raise ValueError('not a regular file/directory or hard-linked upload source')
    return info


def _child(parent, name, *, directory=False, create=False, security=None, metadata_only=False):
    component(name)
    size = len(name.encode('utf-16-le'))
    buffer = c.create_unicode_buffer(name, size // 2 + 1)
    string = UnicodeString(size, size + 2, c.cast(buffer, w.LPWSTR))
    attrs = ObjectAttributes(c.sizeof(ObjectAttributes), parent, c.pointer(string),
                             0x40 | 0x1000, security, None)  # CASE_INSENSITIVE | DONT_REPARSE
    handle, status = w.HANDLE(), IoStatus()
    access = 0x100000 | 0x80  # SYNCHRONIZE, READ_ATTRIBUTES
    if not metadata_only:
        access |= 1  # READ_DATA/LIST_DIRECTORY
    if directory:
        access |= 0x20  # FILE_TRAVERSE
    if create and not directory:
        access |= 0x10000 | 2  # DELETE, WRITE_DATA/ADD_FILE
    options = 0x20 | 0x200000 | (1 if directory else 0x40)  # synchronous, open reparse, dir/non-dir
    result = _ntcreate(c.byref(handle), access, c.byref(attrs), c.byref(status), None,
                       0, 1, 2 if create else 1, options, None, 0)  # share-read only; CREATE or OPEN
    if result < 0:
        raise c.WinError(_dos_error(result))
    try:
        _validate(handle, directory)
    except BaseException:
        _close(handle)
        raise
    return handle


@contextmanager
def parent(path, *, create=False, _verify_at=None):
    path = absolute_path(path)
    if _drive_type(path.anchor) != 3:  # DRIVE_FIXED; no network/removable/UNC
        raise ValueError('Windows SDK supports local fixed NTFS volumes only')
    root = _create('\\\\?\\' + path.anchor, 0x80 | 0x20, 3, None, 3, 0x02000000 | 0x00200000, None)
    if root == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    with ExitStack() as stack:
        stack.enter_context(_owned(root))
        _validate(root, True)
        fs = c.create_unicode_buffer(32)
        _check(_volume(root, None, 0, None, None, None, fs, len(fs)))
        if fs.value.upper() != 'NTFS':
            raise ValueError('Windows SDK supports NTFS only')
        security = stack.enter_context(_private_security()) if create else None
        current = root
        for depth, part in enumerate(path.parts[1:-1], start=2):
            try:
                child = _child(current, part, directory=True)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    child = _child(current, part, directory=True, create=True, security=security)
                except FileExistsError:
                    child = _child(current, part, directory=True)
            current = stack.enter_context(_owned(child))
            if _verify_at is not None:
                _verify_at(depth, current)
        yield current, path.name


def _delete(handle):
    flag = w.BOOL(True)
    _check(_set_ex(handle, 4, c.byref(flag), c.sizeof(flag)))  # FileDispositionInfo


@contextmanager
def _stream(handle, mode, *, remove_on_error=False):
    try:
        fd = msvcrt.open_osfhandle(handle.value, os.O_BINARY | (os.O_RDWR if mode == 'wb' else os.O_RDONLY))
    except BaseException:
        try:
            if remove_on_error:
                _delete(handle)
        finally:
            _close(handle)
        raise
    try:
        stream = os.fdopen(fd, mode)
    except BaseException:
        try:
            if remove_on_error:
                _delete(handle)
        finally:
            os.close(fd)
        raise
    try:
        yield stream
        if mode == 'wb':
            stream.flush()
    except BaseException:
        if remove_on_error:
            _delete(handle)  # exact opened object, never unlink a replacement pathname
        raise
    finally:
        stream.close()  # closes the transferred HANDLE


@contextmanager
def new_file(path):
    with parent(path, create=True) as (folder, name), _private_security() as security:
        handle = _child(folder, name, create=True, security=security)
        with _stream(handle, 'wb', remove_on_error=True) as stream:
            yield stream


def _input_identity(handle, directory):
    info = _validate(handle, directory)
    times = FileBasicInfo()
    _check(_get_ex(handle, 0, c.byref(times), c.sizeof(times)))  # FileBasicInfo
    return ['ntfs-v1', info.volume, (info.index_high << 32) | info.index_low,
            directory, (info.size_high << 32) | info.size_low,
            times.created, times.modified, times.changed]


def input_identity(path, *, directory=False):
    """Capture an approval binding from metadata only, without reading file bytes."""
    with parent(path) as (folder, name):
        with _owned(_child(folder, name, directory=directory, metadata_only=True)) as handle:
            return _input_identity(handle, directory)


@contextmanager
def open_file(path, *, grant=None):
    """Open a file; if supplied, verify (approved path, identity, is_directory) on the retained handle."""
    verify_at = None
    if grant is not None:
        approved, identity, directory = grant
        approved, path = absolute_path(approved), absolute_path(path)
        if not (path.is_relative_to(approved) and (path != approved if directory else path == approved)):
            raise ValueError('input is outside the approved path')

        def verify_at(depth, handle):
            if depth == len(approved.parts) and _input_identity(handle, directory) != identity:
                raise ValueError('approved input changed; request read permission again')

    with parent(path, _verify_at=verify_at) as (folder, name):
        handle = _child(folder, name)
        try:
            if verify_at is not None:
                verify_at(len(path.parts), handle)
        except BaseException:
            _close(handle)
            raise
        with _stream(handle, 'rb') as stream:
            yield stream


@contextmanager
def new_directory(path):
    with parent(path, create=True) as (folder, name), _private_security() as security:
        with _owned(_child(folder, name, directory=True, create=True, security=security)):
            yield


def _entries(handle):
    buffer = c.create_string_buffer(65536)
    while True:
        if not _get_ex(handle, 14, buffer, len(buffer)):  # FileFullDirectoryInfo
            error = c.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                return
            raise c.WinError(error)
        offset = 0
        while True:
            next_offset = struct.unpack_from('<I', buffer, offset)[0]
            attributes, size = struct.unpack_from('<II', buffer, offset + 56)
            end = offset + 68 + size
            if size % 2 or end > len(buffer) or (next_offset and next_offset < 68 + size):
                raise ValueError('invalid Windows directory response')
            name = buffer.raw[offset + 68:end].decode('utf-16-le')
            if name not in {'.', '..'}:
                component(name)
                if attributes & 0x400:
                    raise ValueError('symlinks and Windows reparse points are not allowed')
                yield name, bool(attributes & 0x10)
            if not next_offset:
                break
            offset += next_offset
            if offset + 68 > len(buffer):
                raise ValueError('invalid Windows directory offset')


def scan_files(path, *, max_files):
    visited = 0
    def walk(handle, prefix, depth):
        nonlocal visited
        if depth > 128:
            raise ValueError('output directory depth limit exceeded')
        for name, directory in _entries(handle):
            visited += 1
            if visited > max_files * 2 + 128:
                raise ValueError('output directory entry limit exceeded')
            relative = f'{prefix}/{name}' if prefix else name
            child = _child(handle, name, directory=directory)
            if directory:
                with _owned(child):
                    yield from walk(child, relative, depth + 1)
            else:
                with _stream(child, 'rb') as stream:
                    yield relative, os.fstat(stream.fileno())
    with parent(path) as (folder, name):
        with _owned(_child(folder, name, directory=True)) as root:
            yield from walk(root, '', 0)
