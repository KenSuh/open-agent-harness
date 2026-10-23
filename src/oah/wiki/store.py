# 위키 작업 단위 — 전용 위키 잠금 안에서 저널을 먼저 확인하고, 노트·로그·롤오버(저널→아카이브 먼저 게시→로그 교체)와 복구를 수행한다.
"""Every wiki command runs inside Wiki.session(): one exclusive wiki lock, then the journal check.

Rollover journals the original and replacement bytes before publishing anything, publishes the new
archive without replacing an existing path, then atomically replaces log.md. `recover()` is the only
way past a pending journal; it completes forward or refuses, and never overwrites unknown bytes.
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from oah.atomic import append_jsonl, read_json, read_jsonl, unlink_durable, write_json_atomic, write_text_atomic
from oah.lockfile import LockTimeout, StateLock
from oah.wiki import lint as lint_module
from oah.wiki import log as log_module
from oah.wiki import notes, tasks
from oah.wiki.base import (EXIT_ENVIRONMENT, EXIT_REFUSED, WikiError, WikiSettings, fsync_folder, publish_new,
                           read_bytes)

JOURNAL = "rollover-journal.json"
MAINTENANCE = "maintenance.jsonl"
RESOLUTIONS = "recovery-resolutions.jsonl"
RESOLVED = "resolved"  # resolved rollover journals, kept with a UTC time stamp in their name
Fault = Callable[[str], None]


def _no_fault(step: str) -> None:
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _identity(path: pathlib.Path) -> tuple[int, int, int, int] | None:
    """Which file a path names right now, and its size and change time."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _entry_key(entry: log_module.Entry) -> bytes:
    """An entry's identity for duplicate and dropped-entry checks: its bytes, CRLF folded, trailing space dropped."""
    return entry.data.replace(b"\r\n", b"\n").rstrip()


def _pending_reason(journal: pathlib.Path) -> str:
    try:
        data = read_json(journal, None)
    except Exception:  # the reason is only for the message; recover validates the journal
        return "unreadable"
    if isinstance(data, dict):
        return f"phase {data.get('phase', '?')}" + (f", reason {data['reason']}" if data.get("reason") else "")
    return "unreadable"


def _decode_fields(data: dict, keys: tuple[str, ...]) -> bool:
    try:
        for key in keys:
            if not isinstance(data.get(key), str):
                return False
            data[key] = base64.b64decode(data[key], validate=True)
    except ValueError:
        return False
    return True


@dataclass
class MaintenanceCheck:
    """What `oah finish` needs: ok is False when task maintenance is missing or stale."""
    ok: bool
    problems: list[str] = field(default_factory=list)
    record: dict | None = None
    lint: str | None = None
    hygiene: str | None = None


@dataclass(frozen=True)
class RolloverResult:
    line: str
    archive: pathlib.Path | None


class Wiki:
    def __init__(self, root: pathlib.Path, settings: WikiSettings, state: pathlib.Path, *,
                 lock_timeout: float = 10.0) -> None:
        self.root = pathlib.Path(root)
        self.lock_timeout = lock_timeout
        self.settings = settings
        self.dir = self.root / settings.dir
        self.state = pathlib.Path(state) / "wiki"
        self.lock_path = self.state / ".lock"
        self.journal_path = self.state / JOURNAL

    @contextlib.contextmanager
    def session(self, *, recovering: bool = False) -> Iterator[None]:
        """Hold the wiki lock; refuse OAH_WIKI_RECOVERY_PENDING before touching any wiki file."""
        lock = StateLock(self.lock_path, timeout=self.lock_timeout)
        try:
            lock.__enter__()
        except LockTimeout as exc:
            raise WikiError("OAH_WIKI_BUSY", f"another oah wiki command held {self.lock_path} for "
                            f"{self.lock_timeout:g}s; nothing was changed. Wait for it to finish, then retry.",
                            EXIT_REFUSED) from exc
        try:
            if not recovering and read_bytes(self.journal_path) is not None:
                raise WikiError("OAH_WIKI_RECOVERY_PENDING", f"an interrupted rollover left {self.journal_path} "
                                f"({_pending_reason(self.journal_path)}). Nothing was read or changed. Run "
                                "`oah wiki recover` to finish it, then retry.", EXIT_ENVIRONMENT)
            yield
        finally:
            lock.__exit__(None, None, None)

    def _require(self, *names: str) -> None:
        if not self.dir.is_dir():
            raise WikiError("OAH_WIKI_MISSING", f"{self.dir} does not exist. Run `oah init` to create the wiki, "
                            "or set [wiki] dir in .oah/config.toml.", EXIT_ENVIRONMENT)
        for name in names:
            if not (self.dir / name).is_file():
                raise WikiError("OAH_WIKI_MISSING", f"{self.dir / name} does not exist. Run `oah init` to "
                                "scaffold the wiki files, then retry.", EXIT_ENVIRONMENT)

    # ---- notes and log -------------------------------------------------------------------------

    def new(self, title: str, *, kind: str = "note", sources=(), tags=(),
            today: datetime.date | None = None) -> pathlib.Path:
        today = today or datetime.date.today()
        folder, label = notes.KINDS[kind]
        with self.session():
            self._require("index.md")
            title = notes.validate_title(title)
            slug = notes.slugify(title)
            existing = {path.stem.casefold(): path for path in lint_module.markdown_files(self.dir)}
            if slug.casefold() in existing:
                raise WikiError("OAH_WIKI_NOTE_EXISTS", f"{existing[slug.casefold()]} already uses the name "
                                f"{slug!r}; pick a different title, or edit that note", EXIT_REFUSED)
            custom = self.dir / "templates" / f"{kind}.md"
            template = (custom.read_bytes().decode("utf-8") if custom.is_file()
                        else notes.packaged("wiki", f"{kind}.md"))
            text = notes.render_note(template, title=title, today=today, sources=list(sources), tags=list(tags))
            index_path = self.dir / "index.md"
            index = notes.add_to_index(index_path.read_bytes(), label, notes.index_item(slug, title))
            path = self.dir / folder / f"{slug}.md"
            if not publish_new(path, text.encode("utf-8")):
                raise WikiError("OAH_WIKI_NOTE_EXISTS", f"{path} already exists; pick a different title",
                                EXIT_REFUSED)
            write_text_atomic(index_path, index.decode("utf-8"))
            return path

    def log(self, title: str, *, body: str = "", today: datetime.date | None = None) -> log_module.Entry:
        today = today or datetime.date.today()
        with self.session():
            return self._log(title, body, today)

    def _log(self, title: str, body: str, today: datetime.date) -> log_module.Entry:
        self._require("log.md")
        title = notes.validate_title(title)
        path = self.dir / "log.md"
        updated = log_module.add_entry(path.read_bytes(), title=title, body=body, date=today)
        write_text_atomic(path, updated.decode("utf-8"))
        return log_module.parse_log(updated, "log.md").entries[0]

    # ---- rollover and recovery -----------------------------------------------------------------

    def _archives(self, folder: pathlib.Path) -> dict[int, tuple[str, bytes]]:
        naming = self.settings.naming
        found: dict[int, tuple[str, bytes]] = {}
        for path in sorted(folder.iterdir()):
            number = naming.number(path.name)
            if number is None or not path.is_file():
                continue
            if not naming.well_formed(path.name) or number in found:
                raise WikiError("OAH_WIKI_LOG_INVALID", f"{path.name} does not fit the archive numbering of "
                                f"{naming.prefix}NN.md (each number once, starting at 01); rename it by hand, then "
                                "retry", EXIT_REFUSED)
            found[number] = (path.name, path.read_bytes())
        return found

    def _digests(self, folder: pathlib.Path, skip: str = "") -> dict[str, str]:
        return {name: _sha(data) for name, data in self._archives(folder).values() if name != skip}

    def rollover(self, *, today: datetime.date | None = None, fault: Fault = _no_fault, apply: bool = True,
                 before_write: Callable[[log_module.RolloverPlan], None] | None = None) -> RolloverResult:
        """Plan (apply=False: a dry run that writes nothing) or apply a rollover under the wiki lock.

        before_write runs after planning and before the first write, so a caller can refuse, for
        example when no lease covers the files.
        """
        today = today or datetime.date.today()
        with self.session():
            return self._rollover(today, fault, apply=apply, before_write=before_write)

    def _stream(self, log_data: bytes | None) -> bytes | None:
        """The current preservation stream, or None when it cannot be read."""
        try:
            archives = {n: data for n, (_, data) in self._archives(self.dir).items()}
            return log_module.preservation_stream(log_data or b"", archives, self.settings.naming)
        except (WikiError, OSError):
            return None

    def _rollover(self, today: datetime.date, fault: Fault, *, apply: bool = True,
                  before_write: Callable[[log_module.RolloverPlan], None] | None = None) -> RolloverResult:
        self._require("log.md")
        log_path = self.dir / "log.md"
        identity = _identity(log_path)
        original = log_path.read_bytes()
        archives = self._archives(self.dir)
        numbered = {n: data for n, (_, data) in archives.items()}
        plan = log_module.plan_rollover(original, numbered, trigger=self.settings.rollover_trigger_bytes,
                                        target=self.settings.rollover_target_bytes, today=today,
                                        naming=self.settings.naming)
        if isinstance(plan, log_module.RolloverNoOp):
            extra = f" oversized={'; '.join(plan.oversized)}" if plan.oversized else ""
            return RolloverResult(f"OAH_WIKI_ROLLOVER_NOOP {plan.reason}{extra}", None)
        if not apply:
            return RolloverResult(f"OAH_WIKI_ROLLOVER_PLAN {plan.summary()} (dry run: nothing written; "
                                  "`oah wiki rollover --apply` writes it)", None)
        if before_write is not None:
            before_write(plan)
        stream = log_module.preservation_stream(original, numbered, self.settings.naming)
        archive_path = self.dir / plan.archive_name
        snapshot = {name: _sha(data) for name, data in archives.values()}
        journal = {
            "version": 1, "phase": "staged", "created_at": _now(), "wiki_dir": str(self.dir.resolve()),
            "log": "log.md", "archive": plan.archive_name, "archive_set": snapshot,
            "original_log": _b64(original), "original_log_sha256": _sha(original),
            "replacement_log": _b64(plan.log), "replacement_log_sha256": _sha(plan.log),
            "archive_bytes": _b64(plan.archive), "archive_sha256": _sha(plan.archive),
            "entries_sha256": plan.entries_sha256,
        }
        write_json_atomic(self.journal_path, journal)
        fault("journal-written")
        # Revalidate before publication: the same file (identity) with the same bytes, same archives.
        if (_identity(log_path) != identity or read_bytes(log_path) != original
                or self._digests(self.dir) != snapshot):
            unlink_durable(self.journal_path)
            raise WikiError("OAH_WIKI_CONCURRENT_EDIT", "log.md or an archive changed while rollover was "
                            "preparing; nothing was published. Retry when no one is editing the log.",
                            EXIT_REFUSED)
        fault("validated")
        if not publish_new(archive_path, plan.archive):
            unlink_durable(self.journal_path)
            raise WikiError("OAH_WIKI_CONCURRENT_EDIT", f"{archive_path.name} appeared while rollover was "
                            "running; nothing was replaced. Retry.", EXIT_REFUSED)
        fault("archive-published")
        write_json_atomic(self.journal_path, dict(journal, phase="archive-published"))
        fault("phase-recorded")
        write_text_atomic(log_path, plan.log.decode("utf-8"))
        published = _identity(log_path)
        fault("log-replaced")
        # After publication: re-read both files. Only the exact published file and bytes, with an
        # unchanged preservation stream, let the journal go. Anything else is recorded, never touched.
        log_now, archive_now = read_bytes(log_path), read_bytes(archive_path)
        if (_identity(log_path) == published and log_now == plan.log and archive_now == plan.archive
                and self._stream(log_now) == stream):
            unlink_durable(self.journal_path)
            return RolloverResult(f"OAH_WIKI_ROLLED_OVER {plan.summary()}", archive_path)
        live = {"phase": "changed-after-publication", "reason": "changed-after-publication",
                "detected_at": _now(), "current_log": _b64(log_now or b""), "current_log_sha256": _sha(log_now or b"")}
        if archive_now is not None:
            live.update(current_archive=_b64(archive_now), current_archive_sha256=_sha(archive_now))
        write_json_atomic(self.journal_path, dict(journal, **live))
        differ = self._differing(log_path, archive_path, plan.log, plan.archive)
        raise WikiError("OAH_WIKI_RECOVERY_PENDING", f"{differ} changed right after rollover published them. "
                        f"No file was touched; the live bytes are recorded in {self.journal_path} (reason "
                        "changed-after-publication). Run `oah wiki recover` for what to reconcile.", EXIT_ENVIRONMENT)

    def _differing(self, log_path: pathlib.Path, archive_path: pathlib.Path, log: bytes, archive: bytes) -> str:
        names = [path.name for path, data in ((log_path, log), (archive_path, archive)) if read_bytes(path) != data]
        return " and ".join(names) or f"{log_path.name} (another file now has its name)"

    def _load_journal(self) -> dict | None:
        data = read_json(self.journal_path, None)
        if data is None:
            return None
        corrupt = WikiError("OAH_WIKI_JOURNAL_CORRUPT", f"{self.journal_path} is not a valid rollover journal. "
                            "oah never resets it: compare log.md and the archives with your last commit, "
                            "repair them, then move the journal away by hand.", EXIT_ENVIRONMENT)
        keys = ("wiki_dir", "log", "archive", "original_log", "replacement_log", "archive_bytes")
        if not (isinstance(data, dict) and data.get("version") == 1 and all(isinstance(data.get(k), str) for k in keys)
                and isinstance(data.get("archive_set"), dict)):
            raise corrupt
        optional = tuple(key for key in ("current_log", "current_archive") if key in data)
        if not _decode_fields(data, ("original_log", "replacement_log", "archive_bytes", *optional)):
            raise corrupt
        if (any(_sha(data[key]) != data.get(f"{key}_sha256") for key in ("original_log", "replacement_log", *optional))
                or _sha(data["archive_bytes"]) != data.get("archive_sha256")
                or self.settings.naming.number(data["archive"]) is None or data["log"] != "log.md"):
            raise corrupt
        return data

    def recover(self, *, resolved: bool = False, fault: Fault = _no_fault) -> str:
        """Finish an interrupted rollover, or (resolved=True) accept files the user reconciled by hand."""
        with self.session(recovering=True):
            journal = self._load_journal()
            if journal is None:
                return "OAH_WIKI_RECOVER_NOTHING nothing to recover"
            if resolved:
                return self._resolve(journal)
            folder = pathlib.Path(journal["wiki_dir"])
            log_path, archive_path = folder / journal["log"], folder / journal["archive"]
            log_now, archive_now = read_bytes(log_path), read_bytes(archive_path)
            others = self._digests(folder, skip=journal["archive"]) if folder.is_dir() else None
            original, replacement, archive = (journal["original_log"], journal["replacement_log"],
                                              journal["archive_bytes"])
            if journal.get("phase") == "changed-after-publication":
                differ = self._differing(log_path, archive_path, replacement, archive)
                raise self._conflict(log_path, archive_path, f"{differ} changed right after rollover published them "
                                     "(reason changed-after-publication)")
            if others == journal["archive_set"]:
                if log_now == replacement and archive_now == archive:
                    unlink_durable(self.journal_path)
                    return f"OAH_WIKI_RECOVERED already complete: {archive_path.name} and log.md match the journal"
                if log_now == original and archive_now in (None, archive):
                    if archive_now is None and not publish_new(archive_path, archive):
                        archive_now = read_bytes(archive_path)
                        if archive_now != archive:
                            raise self._conflict(log_path, archive_path)
                    fault("archive-published")
                    write_text_atomic(log_path, replacement.decode("utf-8"))
                    fault("log-replaced")
                    unlink_durable(self.journal_path)
                    return f"OAH_WIKI_RECOVERED completed: published {archive_path.name} and replaced log.md"
            raise self._conflict(log_path, archive_path)

    def _conflict(self, log_path: pathlib.Path, archive_path: pathlib.Path, what: str = "") -> WikiError:
        what = what or (f"{log_path.name} or the archives ({archive_path.name} and its siblings) no longer match "
                        "the journal")
        return WikiError("OAH_WIKI_RECOVERY_CONFLICT", f"{what}. Nothing was changed. {self.journal_path} keeps the "
                         "original, published and live bytes (base64). Fix log.md and the archives by hand so every "
                         f"entry appears exactly once (in {log_path.parent}), then run `oah wiki recover --resolved`.",
                         EXIT_ENVIRONMENT)

    def _resolve(self, journal: dict) -> str:
        """Accept a manual reconciliation: everything parses and no entry appears twice; else change nothing."""
        folder = pathlib.Path(journal["wiki_dir"])
        problems: list[str] = []
        files: dict[str, bytes] = {}
        try:
            log_data = read_bytes(folder / "log.md")
            if log_data is None:
                raise WikiError("OAH_WIKI_LOG_INVALID", "log.md is missing", EXIT_REFUSED)
            files["log.md"] = log_data
            files.update({name: data for name, data in self._archives(folder).values()})
            seen: dict[bytes, str] = {}
            for name, data in files.items():
                for entry in log_module.parse_log(data, name).entries:
                    key = _entry_key(entry)
                    if key in seen:
                        problems.append(f"'## {entry.date} {entry.title}' is in both {seen[key]} and {name}")
                    seen.setdefault(key, f"{name}:{entry.line}")
        except WikiError as exc:
            problems.append(str(exc))
        if problems:
            raise WikiError("OAH_WIKI_RECOVERY_CONFLICT", "the wiki is not reconciled yet; nothing was changed. "
                            + "; ".join(problems[:10]) + (f" (and {len(problems) - 10} more)" if len(problems) > 10 else "")
                            + ". Fix these by hand, then run `oah wiki recover --resolved` again.", EXIT_ENVIRONMENT)
        missing = self._dropped_entries(journal, set(seen))
        moment = datetime.datetime.now(datetime.timezone.utc)
        kept = self.state / RESOLVED / f"rollover-journal-{moment.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
        kept.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.journal_path, kept)  # the journal bytes are kept, never deleted
        fsync_folder(kept.parent)
        fsync_folder(self.journal_path.parent)
        record = {"version": 1, "at": moment.isoformat(timespec="seconds"), "wiki_dir": str(folder),
                  "phase": journal.get("phase"), "reason": journal.get("reason"), "archive": journal["archive"],
                  "files": {name: _sha(data) for name, data in sorted(files.items())},
                  "resolved_journal": str(kept), "dropped_entries": missing}
        append_jsonl(self.state / RESOLUTIONS, record)
        lines = [f"OAH_WIKI_RECOVERY_RESOLVED {len(files)} files parse and every entry appears once; the journal "
                 f"is kept as {kept}"]
        if missing:
            lines.append(f"OAH_WIKI_RECOVERY_DROPPED {len(missing)} entries from the journal are not in the "
                         f"reconciled files; their bytes stay in {kept}:")
            lines.extend(f"  {item}" for item in missing)
        return "\n".join(lines)

    def _dropped_entries(self, journal: dict, present: set[bytes]) -> list[str]:
        """Entries of the journal's original snapshot or captured live bytes that the reconciled files lack."""
        sources = [("original_log", "log.md"), ("archive_bytes", journal["archive"]),
                   ("current_log", "log.md"), ("current_archive", journal["archive"])]
        missing: list[str] = []
        listed: set[bytes] = set()
        for key, name in sources:
            data = journal.get(key)
            if data is None:
                continue
            try:
                entries = log_module.parse_log(data, name).entries
            except WikiError:
                missing.append(f"{key}: could not be parsed; inspect it in the kept journal")
                continue
            for entry in entries:
                entry_key = _entry_key(entry)
                if entry_key not in present and entry_key not in listed:
                    listed.add(entry_key)
                    missing.append(f"'## {entry.date} {entry.title}' (journal {key}, line {entry.line})")
        return missing

    # ---- task maintenance (oah wiki maintain --task) --------------------------------------------

    def _rel(self, path: pathlib.Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _task_entries(self, task: str) -> list[tuple[str, log_module.Entry]]:
        archives = {name: data for name, data in self._archives(self.dir).values()}
        return tasks.task_entries(task, (self.dir / "log.md").read_bytes(), archives)

    def maintain_task(self, task: str, *, title: str | None = None, body: str = "",
                      today: datetime.date | None = None,
                      steps: tuple[str, ...] = ("log", "index", "rollover")) -> list[str]:
        """Task-keyed log entry, index line and size-based rollover, then one maintenance record.

        Idempotent by task id: an entry or index line that already exists (in log.md or an archive)
        is verified and kept, and only missing work is done. Everything runs under one wiki lock.
        """
        tasks.check_task(task)
        today = today or datetime.date.today()
        title = notes.validate_title(title or f"Task {task}")
        status = {"log": "skipped", "index": "skipped", "rollover": "skipped"}
        lines: list[str] = []
        with self.session():
            self._require("log.md", "index.md")
            archive: pathlib.Path | None = None
            if "log" in steps:
                found = self._task_entries(task)
                if not found:
                    self._log(title, f"{tasks.marker(task)}\n{body}", today)
                status["log"] = "present" if found else "added"
            if "index" in steps:
                index_path = self.dir / "index.md"
                data = index_path.read_bytes()
                present = tasks.index_lines(data, task) > 0
                if not present:
                    line = tasks.index_line(task, title, self.settings.index_line_limit)
                    write_text_atomic(index_path, notes.add_to_index(data, tasks.INDEX_LABEL, line).decode("utf-8"))
                status["index"] = "present" if present else "added"
            if "rollover" in steps:
                result = self._rollover(today, _no_fault)  # under the wiki lock, no lease needed
                lines.append(result.line)
                archive = result.archive
                status["rollover"] = archive.name if archive else "noop"
            record = self._record(task, archive, status)
        lines.append(f"OAH_WIKI_MAINTAINED task={task} log={status['log']} index={status['index']} "
                     f"rollover={status['rollover']} record={self.records_path}")
        return lines

    @property
    def records_path(self) -> pathlib.Path:
        return self.state / MAINTENANCE

    def _record(self, task: str, archive: pathlib.Path | None, status: dict[str, str]) -> dict:
        outputs = [self.dir / "log.md", self.dir / "index.md", *([archive] if archive else [])]
        record = {
            "version": 1, "task": task, "worktree": str(self.root.resolve()),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "outputs": {self._rel(path): _sha(path.read_bytes()) for path in outputs},
            "steps": status,
        }
        append_jsonl(self.records_path, record)
        return record

    def verify_task(self, task: str, *, now: float | None = None) -> "MaintenanceCheck":
        """Read-only: the task's entry, index line and latest record against the current wiki bytes."""
        tasks.check_task(task)
        check = MaintenanceCheck(ok=False)
        hint = f"run `oah wiki maintain --task {task}`"
        with self.session():
            self._require("log.md", "index.md")
            found = self._task_entries(task)
            if len(found) != 1:
                check.problems.append(f"OAH_WIKI_MAINTENANCE_MISSING: {len(found)} log entries carry "
                                      f"{tasks.marker(task)} (expected one log entry); {hint}")
            count = tasks.index_lines((self.dir / "index.md").read_bytes(), task)
            if count != 1:
                check.problems.append(f"OAH_WIKI_MAINTENANCE_MISSING: index.md has {count} '- task {task}:' "
                                      f"lines (expected one); {hint}")
            worktree = str(self.root.resolve())
            records = [item for item in read_jsonl(self.records_path)
                       if item.get("task") == task and item.get("worktree") == worktree]
            if not records:
                check.problems.append(f"OAH_WIKI_MAINTENANCE_MISSING: no maintenance record for task {task} in "
                                      f"this worktree; {hint}")
            else:
                check.record = records[-1]
                outputs = check.record.get("outputs")
                for rel, digest in (outputs.items() if isinstance(outputs, dict) else []):
                    current = read_bytes(self.root / rel)
                    if current is None or _sha(current) != digest:
                        check.problems.append(f"OAH_WIKI_MAINTENANCE_STALE: {rel} changed since the last "
                                              f"maintenance of task {task}; {hint} again")
            now = time.time() if now is None else now
            check.lint = lint_module.scan(self.root, self.dir, self.settings, now=now).summary()
            check.hygiene = lint_module.hygiene(self.root, self.dir, self.settings, self.state / "hygiene.json",
                                                now=now, store=False)
        check.ok = not check.problems
        return check

    # ---- lint and hygiene ----------------------------------------------------------------------

    def lint(self, *, now: float | None = None, index: bool = False) -> lint_module.Report:
        with self.session():
            self._require()
            now = time.time() if now is None else now
            if index:
                report = lint_module.scan_index(self.root, self.dir, self.settings, now=now)
                lint_module.write_candidates(self.state / "index-candidates.json", self.dir, report)
                return report
            return lint_module.scan(self.root, self.dir, self.settings, now=now)

    def hygiene(self, *, now: float | None = None) -> str:
        with self.session():
            self._require()
            now = time.time() if now is None else now
            return lint_module.hygiene(self.root, self.dir, self.settings, self.state / "hygiene.json", now=now)


def wiki_for(root: pathlib.Path, settings: WikiSettings, env: os._Environ | dict, *,
             lock_timeout: float = 10.0) -> Wiki:
    """The Wiki of the repository that contains root, with state under oah.paths.state_dir."""
    from oah.paths import repo_info, state_dir

    info = repo_info(pathlib.Path(root))
    return Wiki(info.root, settings, state_dir(info, env), lock_timeout=lock_timeout)
