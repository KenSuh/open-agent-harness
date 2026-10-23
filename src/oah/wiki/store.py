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
from dataclasses import dataclass
from typing import Callable, Iterator

from oah.atomic import read_json, unlink_durable, write_json_atomic, write_text_atomic
from oah.lockfile import StateLock
from oah.wiki import lint as lint_module
from oah.wiki import log as log_module
from oah.wiki import notes
from oah.wiki.base import EXIT_ENVIRONMENT, EXIT_REFUSED, WikiError, WikiSettings, publish_new, read_bytes

JOURNAL = "rollover-journal.json"
Fault = Callable[[str], None]


def _no_fault(step: str) -> None:
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass(frozen=True)
class RolloverResult:
    line: str
    archive: pathlib.Path | None


class Wiki:
    def __init__(self, root: pathlib.Path, settings: WikiSettings, state: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self.settings = settings
        self.dir = self.root / settings.dir
        self.state = pathlib.Path(state) / "wiki"
        self.lock_path = self.state / ".lock"
        self.journal_path = self.state / JOURNAL

    @contextlib.contextmanager
    def session(self, *, recovering: bool = False) -> Iterator[None]:
        """Hold the wiki lock; refuse OAH_WIKI_RECOVERY_PENDING before touching any wiki file."""
        with StateLock(self.lock_path):
            if not recovering and read_bytes(self.journal_path) is not None:
                raise WikiError("OAH_WIKI_RECOVERY_PENDING", f"an interrupted rollover left {self.journal_path}. "
                                "Nothing was read or changed. Run `oah wiki recover` to finish it, then retry.",
                                EXIT_ENVIRONMENT)
            yield

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
            self._require("log.md")
            title = notes.validate_title(title)
            path = self.dir / "log.md"
            updated = log_module.add_entry(path.read_bytes(), title=title, body=body, date=today)
            write_text_atomic(path, updated.decode("utf-8"))
            return log_module.parse_log(updated, "log.md").entries[0]

    # ---- rollover and recovery -----------------------------------------------------------------

    def _archives(self, folder: pathlib.Path) -> dict[int, tuple[str, bytes]]:
        found: dict[int, tuple[str, bytes]] = {}
        for path in sorted(folder.iterdir()):
            number = log_module.archive_number(path.name)
            if number is None or not path.is_file():
                continue
            digits = path.name[len("log_archive_"):-len(".md")]
            if len(digits) < 2 or number in found or number == 0:
                raise WikiError("OAH_WIKI_LOG_INVALID", f"{path.name} does not fit the archive numbering "
                                "(01, 02, …, each number once); rename it by hand, then retry", EXIT_REFUSED)
            found[number] = (path.name, path.read_bytes())
        return found

    def _digests(self, folder: pathlib.Path, skip: str = "") -> dict[str, str]:
        return {name: _sha(data) for name, data in self._archives(folder).values() if name != skip}

    def rollover(self, *, today: datetime.date | None = None, fault: Fault = _no_fault) -> RolloverResult:
        today = today or datetime.date.today()
        with self.session():
            self._require("log.md")
            log_path = self.dir / "log.md"
            original = log_path.read_bytes()
            archives = self._archives(self.dir)
            plan = log_module.plan_rollover(original, {n: data for n, (_, data) in archives.items()},
                                            trigger=self.settings.rollover_trigger_bytes,
                                            target=self.settings.rollover_target_bytes, today=today)
            if isinstance(plan, log_module.RolloverNoOp):
                extra = f" oversized={'; '.join(plan.oversized)}" if plan.oversized else ""
                return RolloverResult(f"OAH_WIKI_ROLLOVER_NOOP {plan.reason}{extra}", None)
            archive_path = self.dir / plan.archive_name
            snapshot = {name: _sha(data) for name, data in archives.values()}
            journal = {
                "version": 1, "phase": "staged", "created_at": datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec="seconds"), "wiki_dir": str(self.dir.resolve()), "log": "log.md",
                "archive": plan.archive_name, "archive_set": snapshot,
                "original_log": _b64(original), "original_log_sha256": _sha(original),
                "replacement_log": _b64(plan.log), "replacement_log_sha256": _sha(plan.log),
                "archive_bytes": _b64(plan.archive), "archive_sha256": _sha(plan.archive),
            }
            write_json_atomic(self.journal_path, journal)
            fault("journal-written")
            if read_bytes(log_path) != original or self._digests(self.dir) != snapshot:
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
            fault("log-replaced")
            unlink_durable(self.journal_path)
            extra = f" oversized={'; '.join(plan.oversized)}" if plan.oversized else ""
            return RolloverResult(f"OAH_WIKI_ROLLED_OVER archive={plan.archive_name} moved={plan.moved} "
                                  f"kept={plan.kept} retained_bytes={plan.retained_bytes}{extra}", archive_path)

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
        try:
            for key in ("original_log", "replacement_log", "archive_bytes"):
                data[key] = base64.b64decode(data[key], validate=True)
        except ValueError as exc:
            raise corrupt from exc
        if (_sha(data["original_log"]) != data.get("original_log_sha256")
                or _sha(data["replacement_log"]) != data.get("replacement_log_sha256")
                or _sha(data["archive_bytes"]) != data.get("archive_sha256")
                or log_module.archive_number(data["archive"]) is None or data["log"] != "log.md"):
            raise corrupt
        return data

    def recover(self, *, fault: Fault = _no_fault) -> str:
        with self.session(recovering=True):
            journal = self._load_journal()
            if journal is None:
                return "OAH_WIKI_RECOVER_NOTHING nothing to recover"
            folder = pathlib.Path(journal["wiki_dir"])
            log_path, archive_path = folder / journal["log"], folder / journal["archive"]
            log_now, archive_now = read_bytes(log_path), read_bytes(archive_path)
            others = self._digests(folder, skip=journal["archive"]) if folder.is_dir() else None
            original, replacement, archive = (journal["original_log"], journal["replacement_log"],
                                              journal["archive_bytes"])
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

    def _conflict(self, log_path: pathlib.Path, archive_path: pathlib.Path) -> WikiError:
        return WikiError("OAH_WIKI_RECOVERY_CONFLICT", f"{log_path} or the archives ({archive_path.name} and its "
                         f"siblings) no longer match {self.journal_path}. Nothing was changed. Compare them with the "
                         "journal's original and replacement bytes, reconcile by hand, then move the journal away.",
                         EXIT_ENVIRONMENT)

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


def wiki_for(root: pathlib.Path, settings: WikiSettings, env: os._Environ | dict) -> Wiki:
    """The Wiki of the repository that contains root, with state under oah.paths.state_dir."""
    from oah.paths import repo_info, state_dir

    info = repo_info(pathlib.Path(root))
    return Wiki(info.root, settings, state_dir(info, env))
