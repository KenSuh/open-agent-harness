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
import sys
import traceback
from unittest import mock
from typing import Callable


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
    phase: str = 'before'
    engine_spawns: int = 0
    runner_creations: int = 0
    claim_present: bool = False
    file_occurrence: int = 0
    reader_scope: tuple[str, ...] = ()


class ObservedFiles:
    def __init__(self, state, *, extra=(), fault=None, target=None, defer_content=False,
                 followup=None, persistent=True):
        self.state = os.path.abspath(state)
        self.extra = {os.path.abspath(path) for path in extra}
        self.fault, self.target = fault, target
        self.defer_content = defer_content
        self.followup, self.persistent = followup, persistent
        self.followup_reached = []
        self.followup_retained = {}
        self.record_refusals = []
        self.publication_diagnostics = []
        self.accesses = []
        self.counts = {}
        self.file_counts = {}
        self.reached = []
        self.content_accesses = []
        self.content_retained = {}
        self.content_writes = {}
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
        self.observed_path = None
        self.retained = {}
        self.phase: Callable[[], tuple[str, int, int]] = lambda: ('before', 0, 0)

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

    @classmethod
    def query_key(cls, access):
        return (cls.key(access), access.operation, access.site, access.chain, access.reader_scope,
                access.phase, access.engine_spawns, access.runner_creations)

    def snapshot(self):
        self.active = False
        try:
            retained = {}
            current = {str(item) for item in pathlib.Path(self.state).rglob('*') if item.is_file()}
            for previous in self.initial | current:
                if self.selected(previous):
                    try:
                        with self.raw_open(previous, 'rb') as handle:
                            retained[pathlib.Path(previous)] = handle.read()
                    except FileNotFoundError:
                        continue
            return retained
        finally:
            self.active = True

    def write_content(self, path, replacement):
        with self.raw_open(path, 'wb') as handle:
            handle.write(replacement)
        self.content_writes[path] = replacement

    def observe(self, path, operation):
        if not self.active or not self.selected(path):
            return None
        existing = path in self.initial or any(path.startswith(folder + os.sep) for folder in self.earlier_runs)
        normalized = path if existing else re.sub(r'r[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', '{new-run}', path)
        file_key = (normalized, operation)
        file_occurrence = self.file_counts.get(file_key, 0) + 1
        self.file_counts[file_key] = file_occurrence
        if self.target is not None and path != self.observed_path and (normalized, operation) != (
                self.key(self.target), self.target.operation):
            return None
        frames = traceback.extract_stack()
        read_at = next((index for index, frame in enumerate(frames)
            if frame.filename.replace('\\', '/').endswith('/discovery/records.py')
            and frame.name == '_read'), None)
        reader_scope = tuple(pathlib.Path(frame.filename).name + ':' + frame.name + ':' + str(frame.lineno)
            for frame in frames[:read_at + 1] if '/src/oah/' in frame.filename.replace('\\', '/')) \
            if read_at is not None else ()
        owned = any(frame.filename.replace('\\', '/').endswith('/discovery/records.py') for frame in frames)
        input_owned = any(frame.filename.replace('\\', '/').endswith('/discovery/inputs.py') for frame in frames)
        callers = [frame for frame in frames if '/src/oah/' in frame.filename.replace('\\', '/')
                   and not frame.filename.replace('\\', '/').endswith('/discovery/records.py')]
        site = ':'.join((pathlib.Path(callers[-1].filename).name, callers[-1].name,
                         str(callers[-1].lineno))) if callers else 'outside-oah'
        try:
            self.raw_lstat(path)
            present = True
        except FileNotFoundError:
            present = False
        key = (normalized, operation, site)
        occurrence = self.counts.get(key, 0) + 1
        self.counts[key] = occurrence
        chain = tuple(pathlib.Path(frame.filename).name + ':' + frame.name for frame in callers)
        phase, engine_spawns, runner_creations = self.phase()
        try:
            self.raw_lstat(os.path.join(os.path.dirname(path), 'worker.claim'))
            claim_present = True
        except FileNotFoundError:
            claim_present = False
        access = Access(path, operation, site, owned, existing, occurrence, chain, input_owned, present,
                        phase, engine_spawns, runner_creations, claim_present, file_occurrence, reader_scope)
        self.accesses.append(access)
        if self.followup is not None and self.reached and self.query_key(access) == self.query_key(self.followup):
            self.followup_reached.append(access)
            if len(self.followup_reached) == 1:
                self.followup_retained = self.snapshot()
        if self.target is not None and (self.key(access), access.operation, access.site, access.occurrence) == (
                self.key(self.target), self.target.operation, self.target.site, self.target.occurrence):
            self.reached.append(access)
            self.retained = self.snapshot()
            self.failed = path
            self.observed_path = path
        if self.failed == path:
            if not self.persistent:
                self.failed = None
            number = {'missing': errno.ENOENT, 'not-regular': errno.EISDIR, 'denied': errno.EACCES,
                      'eio': errno.EIO}.get(self.fault or '')
            if number is not None:
                kind = PermissionError if self.fault == 'denied' else OSError
                raise kind(number, 'fixture stored-record access failed', path)
            if self.fault == 'invalid-content':
                byte_operations = ('open', 'read', 'os.open', 'os.read')
                if operation not in byte_operations and (self.defer_content or self.target is not None
                        and self.target.operation in byte_operations):
                    return None
                if operation in ('read', 'os.read'):
                    self.content_accesses.append(access)
                    if len(self.content_accesses) == 1:
                        self.content_retained = self.snapshot()
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
            def __init__(self, handle, path, mode):
                self.handle, self.path, self.mode = handle, path, mode

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
                    return replacement if 'b' in self.mode else replacement.decode('utf-8')
                return self.handle.read(*args)

        def opened(operation, raw, mode='r', *args, **kwargs):
            path = self.path(raw)
            reading = isinstance(mode, str) and not any(flag in mode for flag in 'wax+')
            if reading:
                self.observe(path, 'open')
            handle = operation(raw, mode, *args, **kwargs)
            return Reader(handle, path, mode) if reading and self.selected(path) else handle

        def stated(operation, raw, *args, **kwargs):
            path = self.path(raw, kwargs.get('dir_fd'))
            replacement = self.observe(path, 'stat' if operation is real_stat else 'lstat')
            if replacement is not None and path is not None:
                pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
                self.write_content(path, replacement)
            return operation(raw, *args, **kwargs)

        def os_open(raw, flags, *args, **kwargs):
            path = self.path(raw, kwargs.get('dir_fd'))
            if not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                replacement = self.observe(path, 'os.open')
                if replacement is not None and path is not None:
                    self.write_content(path, replacement)
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
                self.write_content(path, replacement)
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
        records = sys.modules.get('oah.discovery.records')
        if records is not None and hasattr(records, 'RecordRefusal'):
            read_record = records._read

            def recorded(*args, **kwargs):
                try:
                    return read_record(*args, **kwargs)
                except records.RecordRefusal as exc:
                    self.record_refusals.append(dict(path=self.path(exc.path), category=str(exc.category),
                        issue=str(exc.issue), access_count=len(self.accesses)))
                    raise

            self.stack.enter_context(mock.patch.object(records, '_read', recorded))
        fix = sys.modules.get('oah.review.fix')
        if fix is not None:
            diagnostic = fix._error_diagnostic

            def diagnosed(exc):
                trace = traceback.extract_tb(exc.__traceback__)
                stack = traceback.extract_stack()
                if isinstance(exc, OSError) and any(frame.name == '_publication'
                        and frame.filename.replace('\\', '/').endswith('/review/fix.py') for frame in trace) \
                        and any(frame.name == '_guarded'
                        and frame.filename.replace('\\', '/').endswith('/review/fix.py') for frame in stack):
                    self.publication_diagnostics.append(dict(path=self.path(exc.filename),
                        exception=type(exc).__name__, errno=exc.errno, access_count=len(self.accesses),
                        trace=[dict(file=pathlib.Path(frame.filename).name, function=frame.name, line=frame.lineno)
                               for frame in trace if '/src/oah/' in frame.filename.replace('\\', '/')]))
                return diagnostic(exc)

            self.stack.enter_context(mock.patch.object(fix, '_error_diagnostic', diagnosed))
        return self

    def __exit__(self, *args):
        self.stack.__exit__(*args)
