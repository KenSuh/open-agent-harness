# 리뷰 라운드 판정 — 한 시리즈(task, base)의 영수증·수정 시도·라운드 기록을 모아, 디스패치 전 관문과 출력의 이력 참조 검사, 수렴·추가 라운드·결정 정지를 한곳에서 정한다.
"""Series state and every decision rule of the review loop (PLAN 5.9 steps 2 and 7, A22).

Series
    `series(records, log, base, head=..., is_ancestor=...)` keeps the receipts whose `base` is the series
    base, the correction attempts on them, those records in index order as the series history, and the
    round-log lines of this base. Three things are task-wide, so a new base never clears them: the class
    definitions and attempted classes (a class tried in an earlier series is still tried), and the latest
    receipt the waits below look at. History problems (index and record problems, broken attempts) block
    every series and no decision overrides them. Ambiguities are not problems: when a correction on the
    task's latest receipt no longer descends from that receipt's head or is not in the reviewed history
    (an amend, squash or rebase), the user decides.
Gate, before any paid call
    `gate(...)` raises `OAH_REVIEW_NEEDS_DECISION` for history problems or while the round waits
    for a decision (`waiting`: the task's latest receipt stopped for one, or requested changes whose P0/P1
    findings have no finished correction attempt; an ambiguity; or an uncounted call that stopped for a
    decision). With no wait it returns `"converged"` for this series' cached approval at this head.
    Otherwise it raises `OAH_REVIEW_ROUNDS_EXHAUSTED` at `max_rounds` unless `--extra-round`, and
    `OAH_REVIEW_ATTEMPTS_EXHAUSTED` once `round_attempts` calls did not count, unless `--extra-attempt`. A call that
    starts with `--decision` settles the wait for the rest of its round (`round_decision`), until a later
    call stops for a decision again.
Check, after the run
    `check(...)`: the canary line (`OAH_REVIEW_CANARY_MISSING`), the base/head and DB-impact echoes
    (`OAH_REVIEW_OUTPUT_INVALID`), the INTENT and AC lines (`OAH_INTENT_RETURN_INVALID`), then every class
    and prior reference against the series: an earlier round, its receipt digest, its finding and matching
    class ids. Broken references stop for a decision (`OAH_REVIEW_NEEDS_DECISION`); none of these rounds count.
Decide
    No P0/P1 finding ends the series (`converged`, P2/P3 become follow-ups). A P0/P1 finding in a class a
    finished correction attempt tried (by id, or by any class whose normalized mechanism and rule equal
    that class's), one that needs a design or stored-format change, or one outside the task scope stops
    the series (`needs-decision`). Otherwise the series continues (`changes-requested`).
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Sequence

from oah import intent as oah_intent
from oah.review import ReviewError, canary
from oah.review.receipts import SHA_RE, Attempt, Entry, Records, attempts as read_attempts, history_digest
from oah.review.schema import BLOCKING, Output, OutputInvalid, definition_key
from oah.review.settings import ReviewSettings

NEEDS_DECISION = "OAH_REVIEW_NEEDS_DECISION"
CONVERGED = "converged"


@dataclasses.dataclass(frozen=True)
class Series:
    task: str
    base: str
    receipts: tuple[Entry, ...]  # this series
    attempts: tuple[Attempt, ...]  # on this series' receipts
    history: tuple[Entry, ...]
    problems: tuple[str, ...]
    ambiguities: tuple[str, ...]
    next_number: int
    log: tuple[dict, ...]
    task_receipts: tuple[Entry, ...] = ()
    task_attempts: tuple[Attempt, ...] = ()

    @property
    def series_round(self) -> int:
        return len(self.receipts) + 1

    @property
    def latest(self) -> Entry | None:
        return self.receipts[-1] if self.receipts else None

    @property
    def task_latest(self) -> Entry | None:
        return self.task_receipts[-1] if self.task_receipts else None

    def history_items(self) -> list[dict]:
        return [{"kind": entry.kind, "name": entry.name, "digest": entry.digest} for entry in self.history]

    def history_digest(self) -> str:
        return history_digest(self.history_items())

    def classes(self) -> dict[str, dict]:
        """Every class the task's receipts define, by id."""
        found: dict[str, dict] = {}
        for entry in self.task_receipts:
            for item in entry.record.get("classes", []):
                found.setdefault(item["class_id"], item)
        return found

    def attempted(self) -> dict[str, list[tuple[str, str]]]:
        """Class id → (attempt id, outcome) of every finished correction attempt of the task that tried it."""
        found: dict[str, list[tuple[str, str]]] = {}
        for attempt in self.task_attempts:
            if attempt.terminal:
                for class_id in attempt.class_ids:
                    found.setdefault(class_id, []).append((attempt.attempt_id, attempt.outcome))
        return found

    def round_lines(self) -> list[dict]:
        return [line for line in self.log if line.get("series_round") == self.series_round]


@dataclasses.dataclass(frozen=True)
class Decision:
    outcome: str  # converged | changes-requested | needs-decision
    stop_reason: str | None
    follow_ups: tuple[str, ...]


def series(records: Records, log: Sequence[dict], base: str, *, head: str,
           is_ancestor: Callable[[str, str], bool]) -> Series:
    everything = tuple(records.receipts())
    mine = tuple(entry for entry in everything if entry.record.get("base") == base)
    digests = {entry.digest for entry in mine}
    all_attempts = tuple(read_attempts(records))
    chosen = tuple(attempt for attempt in all_attempts if attempt.source_receipt_digest in digests)
    names = {entry.name for entry in mine} | {name for attempt in chosen for name in attempt.names}
    problems = list(records.problems) + [problem for attempt in all_attempts for problem in attempt.problems]
    ambiguities: list[str] = []
    latest = everything[-1] if everything else None
    for attempt in all_attempts:
        if latest is None or attempt.source_receipt_digest != latest.digest or attempt.problems:
            continue  # corrections reviewed by a later round are settled history
        reviewed = latest.record["head"]
        if not is_ancestor(reviewed, attempt.before_head):
            ambiguities.append(f"{attempt.attempt_id} started from {attempt.before_head}, which does not descend from "
                               f"the head {reviewed} that {latest.name} reviewed")
        for label, commit in (("before_head", attempt.before_head), ("after_head", attempt.after_head)):
            if isinstance(commit, str) and SHA_RE.fullmatch(commit) and not is_ancestor(commit, head):
                ambiguities.append(f"{attempt.attempt_id}: its {label} {commit} is not in the reviewed history "
                                   f"(HEAD {head}); the branch was rewritten (amend, squash or rebase) after the "
                                   "correction")
    return Series(task=records.task, base=base, receipts=mine, attempts=chosen,
                  history=tuple(entry for entry in records.entries if entry.name in names),
                  problems=tuple(problems), ambiguities=tuple(ambiguities), next_number=len(everything) + 1,
                  log=tuple(line for line in log if line.get("base") == base), task_receipts=everything,
                  task_attempts=all_attempts)


def _uncorrected(s: Series, latest: Entry) -> list[str]:
    covered = {finding for attempt in s.task_attempts if attempt.terminal
               and attempt.source_receipt_digest == latest.digest for finding in attempt.finding_ids}
    return [item["finding_id"] for item in latest.record.get("findings", [])
            if item.get("severity") in BLOCKING and item["finding_id"] not in covered]


def _decision_current(line: dict, s: Series) -> bool:
    latest = s.task_latest
    target = latest.digest if latest is not None else None
    # Legacy logs settle only their own series' latest receipt. A decision for an older receipt
    # must never clear a later task-wide stop from a different base.
    legacy = target if latest == s.latest else ""
    return line.get("decision_for", legacy) == target


def waiting(s: Series, *, settled: bool = True) -> str | None:
    """Why the next call of this round needs a --decision, or None.

    The task's latest receipt may wait (it stopped for a decision, or requested changes whose P0/P1 findings
    have no finished correction attempt), and so may an ambiguity of this series. A call of this round that
    started with a decision settles the wait (unless `settled` is False), and an uncounted call that stopped
    for a decision waits again."""
    latest = s.task_latest
    reason = None
    if latest is not None and latest.record.get("outcome") == "needs-decision":
        reason = f"{latest.name} stopped the series for a decision: {latest.record.get('decision_stop_reason')}"
    elif latest is not None and latest.record.get("outcome") == "changes-requested" and _uncorrected(s, latest):
        reason = (f"{latest.name} requested changes, but no finished correction attempt is recorded for "
                  f"{', '.join(_uncorrected(s, latest))}, so a recurrence could not be recognized. Record the "
                  "corrections (oah fix records its own; manual corrections need the same records), or decide to go "
                  "on without")
    if reason is None and s.ambiguities:
        reason = ("the correction history is ambiguous: " + "; ".join(s.ambiguities) + ". Decide whether the "
                  "rewritten commits still carry the corrections")
    for line in s.round_lines():
        if line.get("event") == "start" and line.get("decision") and settled and _decision_current(line, s):
            reason = None
        elif line.get("event") == "finish" and not line.get("counted") and line.get("code") == NEEDS_DECISION:
            reason = (f"the last reviewer call of round {s.series_round} did not count and stopped for a decision: "
                      f"{line.get('message', '')}")
    return reason


def round_decision(s: Series) -> str | None:
    """The decision an earlier call of this round started with, while it still settles the round."""
    decision = None
    for line in s.round_lines():
        if line.get("event") == "start" and line.get("decision") and _decision_current(line, s):
            decision = line["decision"]
        elif line.get("event") == "finish" and not line.get("counted") and line.get("code") == NEEDS_DECISION:
            decision = None
    return decision


def gate(s: Series, *, head: str, decision: str | None, extra_round: bool, settings: ReviewSettings,
         extra_attempt: bool = False) -> str | None:
    """Refuse before any paid call; `"converged"` means this head is already approved."""
    if s.problems:
        raise ReviewError(NEEDS_DECISION, "the review history of this series is incomplete or was changed, so "
                          "recurrence cannot be judged and nothing was launched:\n- " + "\n- ".join(s.problems) +
                          "\nRestore or complete the records first (a --decision does not override this), then run "
                          f"oah review --task {s.task} again")
    reason = waiting(s)
    if reason is not None and decision is None:
        raise ReviewError(NEEDS_DECISION, f"{reason}. Nothing was launched. Run oah review --task {s.task} "
                          '--decision "<what you decided>" to continue; the decision is recorded')
    latest = s.latest
    if (reason is None and latest is not None and latest.record.get("outcome") == CONVERGED
            and latest.record.get("head") == head):
        return CONVERGED
    if settings.max_rounds is not None and len(s.receipts) >= settings.max_rounds and not extra_round:
        raise ReviewError("OAH_REVIEW_ROUNDS_EXHAUSTED", f"this series has {len(s.receipts)} counted rounds, the "
                          f"[review] max_rounds limit, so nothing was launched. Run oah review --task {s.task} "
                          "--extra-round to continue (it is recorded), or raise or remove max_rounds in "
                          ".oah/config.toml")
    used = sum(1 for line in s.round_lines() if line.get("event") == "start")
    if used >= settings.round_attempts and not extra_attempt:
        raise ReviewError("OAH_REVIEW_ATTEMPTS_EXHAUSTED", f"round {s.series_round} of this series already used "
                          f"{used} reviewer calls that did not count ([review] round_attempts = "
                          f"{settings.round_attempts}), so oah does not call the paid reviewer again. Read the last "
                          "run's stderr.log in the state folder's runs/ to find the cause and fix it, then run "
                          f"oah review --task {s.task} --base {s.base} --extra-attempt to permit one more call "
                          "(recorded). Review settings remain pinned to the base")
    return None


def _reference_problems(output: Output, s: Series, number: int) -> list[str]:
    rounds = {entry.record["round"]: entry for entry in s.receipts}
    known = s.classes()
    new = {item.class_id for item in output.classes}
    problems: list[str] = []
    for finding in output.findings:
        if finding.class_id not in new and finding.class_id not in known:
            problems.append(f"{finding.finding_id} uses class {finding.class_id}, which no earlier round of this "
                            "series defined")
        for ref in finding.prior_refs:
            where = f"{finding.finding_id} refers to R{ref.round} {ref.finding_id}"
            entry = rounds.get(ref.round)
            if entry is None or ref.round >= number:
                problems.append(f"{where}, but R{ref.round} is not an earlier round of this series")
                continue
            if ref.receipt_digest != entry.digest:
                problems.append(f"{where} with digest {ref.receipt_digest}, but that receipt's digest is {entry.digest}")
                continue
            earlier = {item["finding_id"]: item for item in entry.record.get("findings", [])}
            if ref.finding_id not in earlier:
                problems.append(f"{where}, which is not a finding of R{ref.round}")
            elif earlier[ref.finding_id].get("class_id") != ref.class_id:
                problems.append(f"{where} as class {ref.class_id}, but that finding's class is "
                                f"{earlier[ref.finding_id].get('class_id')}")
            elif finding.class_id != ref.class_id:
                problems.append(f"{where} of class {ref.class_id}, but {finding.finding_id} has class "
                                f"{finding.class_id}; a repeated finding keeps its class")
    return problems


def check(output: Output, s: Series, *, number: int, binding: oah_intent.Binding, base: str, head: str,
          canary_value: str, db_line: str) -> None:
    """Raise the code of the first failed check; a round that fails any of them does not count."""
    if not canary.verified(output.evidence, canary_value):
        raise ReviewError("OAH_REVIEW_CANARY_MISSING", f"the reviewer's evidence lacks the line {canary.LINE}<value> "
                          f"for this snapshot's {canary.FILE_NAME}, so this round does not count")
    problems = [f"{name} is {given!r}, not the reviewed {name} {wanted}"
                for name, given, wanted in (("base", output.base, base), ("head", output.head, head)) if given != wanted]
    if output.db_impact.strip() != db_line:
        problems.append(f"db_impact is {output.db_impact.strip()!r}, not the DB impact line of the brief")
    if problems:
        raise OutputInvalid(problems)
    returned = oah_intent.check_return(output.intent_return, binding)
    if returned:
        raise ReviewError("OAH_INTENT_RETURN_INVALID", "the reviewer's intent_return does not match the bound intent, "
                          "so this round does not count: " + "; ".join(item.message for item in returned))
    broken = _reference_problems(output, s, number)
    if broken:
        raise ReviewError(NEEDS_DECISION, "the reviewer's references to the series history are broken, so this round "
                          "does not count and the series stops for a decision: " + "; ".join(broken) +
                          f'. Check the history, then run oah review --task {s.task} --decision "<what you decided>"')


def _tries(tried: list[tuple[str, str]]) -> str:
    word = "correction attempt" if len(tried) == 1 else "correction attempts"
    return f"{word} " + ", ".join(f"{attempt} ({outcome})" for attempt, outcome in tried)


def decide(output: Output, s: Series) -> Decision:
    blocking = output.blocking()
    if not blocking:
        return Decision(CONVERGED, None, tuple(finding.finding_id for finding in output.findings))
    attempted = s.attempted()
    classes = s.classes()
    twins = {definition_key(classes[c]["mechanism"], classes[c]["rule"]): c for c in attempted if c in classes}
    resolved = {**classes, **{item.class_id: item.to_json() for item in output.classes}}
    reasons: list[str] = []
    for finding in blocking:
        if finding.class_id in attempted:
            reasons.append(f"{finding.finding_id} repeats class {finding.class_id}, which "
                           f"{_tries(attempted[finding.class_id])} already tried to fix")
        elif finding.class_id in resolved:
            definition = resolved[finding.class_id]
            twin = twins.get(definition_key(definition["mechanism"], definition["rule"]))
            if twin is not None:
                reasons.append(f"{finding.finding_id} uses {finding.class_id} with the mechanism and rule of class "
                               f"{twin}, which {_tries(attempted[twin])} already tried to fix")
        if finding.fix_kind != "local":
            reasons.append(f"{finding.finding_id} needs a {finding.fix_kind} change")
        if not finding.in_scope:
            reasons.append(f"{finding.finding_id}'s fix lies outside the task scope")
    if reasons:
        return Decision("needs-decision", "; ".join(reasons), ())
    return Decision("changes-requested", None, ())
