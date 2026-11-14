from __future__ import annotations

import builtins
import contextlib
import dataclasses
import errno
import io
import os
import pathlib
import re
import stat
import traceback
from unittest import mock


@dataclasses.dataclass(frozen=True)
class Access:
    path: str
    operation: str
    site: str
    owned: bool
    existing: bool
    occurrence: int
    chain: tuple[str, ...]
    input_owned: bool
    present: bool


class ObservedFiles:
    def __init__(self, state, *, extra=(), fault=None, target=None):
        self.state = os.path.abspath(state)
        self.extra = {os.path.abspath(path) for path in extra}
        self.fault, self.target = fault, target
        self.accesses = []
        self.counts = {}
        self.reached = []
        self.active = True
        self.descriptors = {}
        self.replaced_read = set()
        self.stack = contextlib.ExitStack()
        self.raw_lstat = os.lstat
        self.raw_open = builtins.open
        self.initial = {os.path.abspath(path) for path in pathlib.Path(state).rglob('*') if path.is_file()} | self.extra
        self.earlier_runs = {str(pathlib.Path(path).parent) for path in self.initial
                             if re.fullmatch(r'r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', pathlib.Path(path).parent.name)}
        self.failed = None
        self.retained = {}

    def path(self, raw, dir_fd=None):
        if isinstance(raw, int):
            return self.descriptors.get(raw)
        if not isinstance(raw, (str, bytes, os.PathLike)):
            return None
        text = os.fsdecode(raw)
        if dir_fd is not None and dir_fd in self.descriptors:
            text = os.path.join(self.descriptors[dir_fd], text)
        return os.path.abspath(text)

    def selected(self, path):
        if path is None:
            return False
        selected = (path in self.extra or path.startswith(self.state + os.sep) and (
            os.sep + 'skills' + os.sep in path or os.sep + 'runs' + os.sep in path or
            os.sep + 'requests' + os.sep in path)
            and (pathlib.Path(path).suffix in ('.json', '.jsonl', '.md') or
                 os.sep + 'sources' + os.sep + 'blobs' + os.sep in path))
        if not selected:
            return False
        try:
            return not stat.S_ISDIR(self.raw_lstat(path).st_mode)
        except FileNotFoundError:
            return True

    @staticmethod
    def key(access):
        return access.path if access.existing else re.sub(
            r'r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', '{new-run}', access.path)

    def observe(self, path, operation):
        if not self.active or not self.selected(path):
            return None
        frames = traceback.extract_stack(limit=35)
        owned = any(frame.filename.endswith('/discovery/records.py') for frame in frames)
        input_owned = any(frame.filename.endswith('/discovery/inputs.py') for frame in frames)
        callers = [frame for frame in frames if '/src/oah/' in frame.filename
                   and not frame.filename.endswith('/discovery/records.py')]
        site = ':'.join((pathlib.Path(callers[-1].filename).name, callers[-1].name,
                         str(callers[-1].lineno))) if callers else 'outside-oah'
        existing = path in self.initial or any(path.startswith(folder + os.sep) for folder in self.earlier_runs)
        try:
            self.raw_lstat(path)
            present = True
        except FileNotFoundError:
            present = False
        key = (path if existing else re.sub(r'r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', '{new-run}', path),
               operation, site)
        occurrence = self.counts.get(key, 0) + 1
        self.counts[key] = occurrence
        chain = tuple(pathlib.Path(frame.filename).name + ':' + frame.name for frame in callers)
        access = Access(path, operation, site, owned, existing, occurrence, chain, input_owned, present)
        self.accesses.append(access)
        if self.target is not None and (self.key(access), access.operation, access.site, access.occurrence) == (
                self.key(self.target), self.target.operation, self.target.site, self.target.occurrence):
            self.reached.append(access)
            for previous in self.initial | {item.path for item in self.accesses}:
                if self.selected(previous):
                    try:
                        with self.raw_open(previous, 'rb') as handle:
                            self.retained[pathlib.Path(previous)] = handle.read()
                    except FileNotFoundError:
                        continue
            self.failed = path
        if self.failed == path:
            number = {'missing': errno.ENOENT, 'not-regular': errno.EISDIR, 'denied': errno.EACCES,
                      'eio': errno.EIO}.get(self.fault or '')
            if number is not None:
                kind = PermissionError if self.fault == 'denied' else OSError
                raise kind(number, 'fixture stored-record access failed', path)
            if self.fault == 'invalid-content':
                return b'\xff'
        return None

    def __enter__(self):
        real_stat, real_lstat = os.stat, os.lstat
        self.raw_lstat = real_lstat
        real_open, real_io, real_os = builtins.open, io.open, os.open
        real_read, real_fstat, real_close = os.read, os.fstat, os.close
        self.raw_open = real_open
        owner = self

        class Reader:
            def __init__(self, handle, path):
                self.handle, self.path = handle, path

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def read(self, *args):
                replacement = owner.observe(self.path, 'read')
                if replacement is not None:
                    return replacement if 'b' in self.handle.mode else replacement.decode('utf-8')
                return self.handle.read(*args)

        def opened(operation, raw, mode='r', *args, **kwargs):
            path = self.path(raw)
            reading = isinstance(mode, str) and not any(flag in mode for flag in 'wax+')
            replacement = self.observe(path, 'open') if reading else None
            if replacement is not None:
                return io.BytesIO(replacement) if 'b' in mode else io.StringIO(replacement.decode('utf-8'))
            handle = operation(raw, mode, *args, **kwargs)
            return Reader(handle, path) if reading and self.selected(path) else handle

        def stated(operation, raw, *args, **kwargs):
            path = self.path(raw, kwargs.get('dir_fd'))
            replacement = self.observe(path, 'stat' if operation is real_stat else 'lstat')
            if replacement is not None and path is not None:
                pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
                with real_open(path, 'wb') as handle:
                    handle.write(replacement)
            return operation(raw, *args, **kwargs)

        def os_open(raw, flags, *args, **kwargs):
            path = self.path(raw, kwargs.get('dir_fd'))
            if not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                replacement = self.observe(path, 'os.open')
                if replacement is not None and path is not None:
                    with real_open(path, 'wb') as handle:
                        handle.write(replacement)
            descriptor = real_os(raw, flags, *args, **kwargs)
            if path is not None:
                self.descriptors[descriptor] = path
            return descriptor

        def os_read(descriptor, size):
            replacement = self.observe(self.path(descriptor), 'os.read')
            if replacement is not None:
                if descriptor in self.replaced_read:
                    return b''
                self.replaced_read.add(descriptor)
                return replacement
            return real_read(descriptor, size)

        def fstat(descriptor):
            path = self.path(descriptor)
            replacement = self.observe(path, 'fstat')
            if replacement is not None and path is not None:
                with real_open(path, 'wb') as handle:
                    handle.write(replacement)
            return real_fstat(descriptor)

        def close(descriptor):
            self.descriptors.pop(descriptor, None)
            self.replaced_read.discard(descriptor)
            return real_close(descriptor)

        self.stack.enter_context(mock.patch.object(builtins, 'open', lambda *a, **k: opened(real_open, *a, **k)))
        self.stack.enter_context(mock.patch.object(io, 'open', lambda *a, **k: opened(real_io, *a, **k)))
        self.stack.enter_context(mock.patch.object(os, 'open', os_open))
        self.stack.enter_context(mock.patch.object(os, 'read', os_read))
        self.stack.enter_context(mock.patch.object(os, 'fstat', fstat))
        self.stack.enter_context(mock.patch.object(os, 'close', close))
        self.stack.enter_context(mock.patch.object(os, 'stat', lambda *a, **k: stated(real_stat, *a, **k)))
        self.stack.enter_context(mock.patch.object(os, 'lstat', lambda *a, **k: stated(real_lstat, *a, **k)))
        return self

    def __exit__(self, *args):
        self.stack.__exit__(*args)
