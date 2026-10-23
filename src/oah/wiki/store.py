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
RECOVERY = ".oah-rollover-recovery.json"  # in the wiki folder; git-ignored by notes.gitignore_snippet()
STASH = ".oah-rollover-stash"  # a second name for the old log.md while it is exchanged
MAINTENANCE = "maintenance.jsonl"
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


def _stash(log_path: pathlib.Path, stash: pathlib.Path) -> bool:
    """Give the old log file a second name, so writes that still reach it after the exchange stay visible.

    False when the file system has no hard links; then only the new log's bytes can be checked.
    """
    try:
        stash.unlink()
    except FileNotFoundError:
        pass
    try:
        os.link(log_path, stash)
    except OSError:
        return False
    fsync_folder(stash.parent)
    return True


def _hash_ok(plan: log_module.RolloverPlan, log_data: bytes | None, archive: bytes | None, naming) -> bool:
    """Read back after writing: kept plus moved entry bytes must hash like the original entries."""
    if log_data is None or archive is None:
        return False
    try:
        both = (log_module.entry_stream(log_data, "log.md")
                + log_module.entry_stream(archive, plan.archive_name))
    except WikiError:
        return False
    return log_module.sha256(both) == plan.entries_sha256


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
            if not recovering:
                for pending in (self.journal_path, self.dir / RECOVERY):
                    if read_bytes(pending) is not None:
                        raise WikiError("OAH_WIKI_RECOVERY_PENDING", f"an interrupted rollover left {pending}. "
                                        "Nothing was read or changed. Run `oah wiki recover` to finish it, then "
                                        "retry.", EXIT_ENVIRONMENT)
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
                                f"{naming.pattern} (each number once, starting at 1); rename it by hand, then "
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

    def _rollover(self, today: datetime.date, fault: Fault, *, apply: bool = True,
                  before_write: Callable[[log_module.RolloverPlan], None] | None = None) -> RolloverResult:
        self._require("log.md")
        log_path = self.dir / "log.md"
        identity = _identity(log_path)
        original = log_path.read_bytes()
        archives = self._archives(self.dir)
        plan = log_module.plan_rollover(original, {n: data for n, (_, data) in archives.items()},
                                        trigger=self.settings.rollover_trigger_bytes,
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
        archive_path = self.dir / plan.archive_name
        stash = self.dir / STASH
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
        # Writer check before the first write: the same file (identity) with the same bytes.
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
        linked = _stash(log_path, stash)
        fault("stashed")
        write_text_atomic(log_path, plan.log.decode("utf-8"))  # the exchange
        fault("log-replaced")
        # Writer check after the exchange: the old file must be unchanged, the new one ours, and the
        # entry bytes must still hash the same.
        old_now = read_bytes(stash) if linked else original
        log_now, archive_now = read_bytes(log_path), read_bytes(archive_path)
        if old_now == original and log_now == plan.log and archive_now == plan.archive and _hash_ok(plan, log_now, archive_now, self.settings.naming):
            if linked:
                unlink_durable(stash)
            unlink_durable(self.journal_path)
            return RolloverResult(f"OAH_WIKI_ROLLED_OVER {plan.summary()}", archive_path)
        if log_now == plan.log and archive_now == plan.archive:
            restore = old_now if old_now is not None else original
            try:
                fault("rollback")
                self._roll_back(log_path, stash if linked else None, restore, archive_path, plan.archive)
            except OSError as exc:
                path = self._save_recovery("rollback-failed", restore, plan, f"{type(exc).__name__}: {exc}")
                raise WikiError("OAH_WIKI_ROLLBACK_FAILED", f"a writer changed log.md during rollover and undoing "
                                f"the rollover failed ({exc}). The bytes log.md must get back are kept in {path}. "
                                "Run `oah wiki recover` to restore them.", EXIT_ENVIRONMENT) from exc
            unlink_durable(self.journal_path)
            raise WikiError("OAH_WIKI_CONCURRENT_EDIT", "a writer changed log.md during rollover; the rollover "
                            "was rolled back and nothing was lost. Retry when no one is editing the log.",
                            EXIT_REFUSED)
        path = self._save_recovery("changed-after-exchange", old_now if old_now is not None else original, plan,
                                   "log.md or the new archive changed right after the exchange")
        raise WikiError("OAH_WIKI_CHANGED_AFTER_EXCHANGE", f"log.md or {archive_path.name} changed right after "
                        f"rollover replaced log.md, so it cannot be undone safely. The earlier bytes are kept in {path}. "
                        "Run `oah wiki recover`; it restores them when log.md is still the rollover's output, and "
                        "otherwise lists what to reconcile by hand.", EXIT_ENVIRONMENT)

    def _roll_back(self, log_path: pathlib.Path, stash: pathlib.Path | None, restore: bytes,
                   archive_path: pathlib.Path, archive: bytes) -> None:
        """Put the old log file back (the very file other writers hold open) and drop the new archive."""
        if stash is not None and read_bytes(stash) == restore:
            os.replace(stash, log_path)
            fsync_folder(log_path.parent)
        else:
            write_text_atomic(log_path, restore.decode("utf-8"))
            if stash is not None:
                unlink_durable(stash)
        if read_bytes(archive_path) == archive:
            unlink_durable(archive_path)

    def _save_recovery(self, reason: str, restore: bytes, plan: log_module.RolloverPlan, detail: str) -> pathlib.Path:
        path = self.dir / RECOVERY
        write_json_atomic(path, {
            "version": 1, "reason": reason, "detail": detail, "created_at": _now(), "log": "log.md",
            "archive": plan.archive_name, "restore_log": _b64(restore), "restore_log_sha256": _sha(restore),
            "replacement_log": _b64(plan.log), "replacement_log_sha256": _sha(plan.log),
            "archive_bytes": _b64(plan.archive), "archive_sha256": _sha(plan.archive),
        })
        return path

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
        if not _decode_fields(data, ("original_log", "replacement_log", "archive_bytes")):
            raise corrupt
        if (_sha(data["original_log"]) != data.get("original_log_sha256")
                or _sha(data["replacement_log"]) != data.get("replacement_log_sha256")
                or _sha(data["archive_bytes"]) != data.get("archive_sha256")
                or self.settings.naming.number(data["archive"]) is None or data["log"] != "log.md"):
            raise corrupt
        return data

    def _load_recovery(self, folder: pathlib.Path) -> dict | None:
        path = folder / RECOVERY
        data = read_json(path, None)
        if data is None:
            return None
        if not (isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("archive"), str)
                and _decode_fields(data, ("restore_log", "replacement_log", "archive_bytes"))
                and _sha(data["restore_log"]) == data.get("restore_log_sha256")
                and _sha(data["replacement_log"]) == data.get("replacement_log_sha256")
                and _sha(data["archive_bytes"]) == data.get("archive_sha256")
                and self.settings.naming.number(data["archive"]) is not None):
            raise WikiError("OAH_WIKI_JOURNAL_CORRUPT", f"{path} is not a valid rollover recovery file. oah never "
                            "resets it: repair log.md by hand from your last commit, then move the file away.",
                            EXIT_ENVIRONMENT)
        return data

    def recover(self, *, fault: Fault = _no_fault) -> str:
        with self.session(recovering=True):
            journal = self._load_journal()
            folder = pathlib.Path(journal["wiki_dir"]) if journal else self.dir
            saved = self._load_recovery(folder)
            if saved is not None:
                return self._recover_saved(folder, saved, journal)
            if journal is None:
                return "OAH_WIKI_RECOVER_NOTHING nothing to recover"
            log_path, archive_path = folder / journal["log"], folder / journal["archive"]
            stash = folder / STASH
            log_now, archive_now = read_bytes(log_path), read_bytes(archive_path)
            stash_now = read_bytes(stash)
            others = self._digests(folder, skip=journal["archive"]) if folder.is_dir() else None
            original, replacement, archive = (journal["original_log"], journal["replacement_log"],
                                              journal["archive_bytes"])
            if others == journal["archive_set"]:
                if (log_now == replacement and archive_now == archive and stash_now is not None
                        and stash_now != original):
                    # The old log took an append before the crash: undo, as the live run would have.
                    self._roll_back(log_path, stash, stash_now, archive_path, archive)
                    unlink_durable(self.journal_path)
                    return "OAH_WIKI_RECOVERED rolled back: the old log.md changed during rollover; it is restored"
                if stash_now not in (None, original) and log_now != replacement:
                    raise self._conflict(log_path, archive_path)
                if log_now == replacement and archive_now == archive:
                    if stash_now is not None:
                        unlink_durable(stash)
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
                    if stash_now is not None:
                        unlink_durable(stash)
                    unlink_durable(self.journal_path)
                    return f"OAH_WIKI_RECOVERED completed: published {archive_path.name} and replaced log.md"
            raise self._conflict(log_path, archive_path)

    def _recover_saved(self, folder: pathlib.Path, saved: dict, journal: dict | None) -> str:
        """Restore log.md from the recovery file a failed rollback left, then drop the new archive."""
        path = folder / RECOVERY
        log_path, archive_path, stash = folder / "log.md", folder / saved["archive"], folder / STASH
        log_now, archive_now = read_bytes(log_path), read_bytes(archive_path)
        restore = saved["restore_log"]
        if log_now not in (restore, saved["replacement_log"]) or archive_now not in (None, saved["archive_bytes"]):
            raise WikiError("OAH_WIKI_RECOVERY_CONFLICT", f"log.md or {archive_path.name} changed after the failed "
                            f"rollback ({saved.get('reason')}). Nothing was changed. {path} keeps the bytes log.md "
                            "should have (restore_log, base64); merge them with the current log.md by hand, delete "
                            f"{archive_path.name} if its entries are still in log.md, then move {path.name} and any "
                            "rollover journal away.", EXIT_ENVIRONMENT)
        self._roll_back(log_path, stash if read_bytes(stash) is not None else None, restore, archive_path,
                        saved["archive_bytes"])
        if read_bytes(stash) is not None:
            unlink_durable(stash)
        if journal is not None:
            unlink_durable(self.journal_path)
        unlink_durable(path)
        return (f"OAH_WIKI_RECOVERED rolled back ({saved.get('reason')}): log.md restored from {path.name} and "
                f"{archive_path.name} removed")

    def _conflict(self, log_path: pathlib.Path, archive_path: pathlib.Path) -> WikiError:
        return WikiError("OAH_WIKI_RECOVERY_CONFLICT", f"{log_path} or the archives ({archive_path.name} and its "
                         f"siblings) no longer match {self.journal_path}. Nothing was changed. Compare them with the "
                         "journal's original and replacement bytes, reconcile by hand, then move the journal away.",
                         EXIT_ENVIRONMENT)

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
