# 리뷰 라운드 판정 — 한 시리즈(task, base)의 영수증·수정 시도·라운드 기록을 모아, 디스패치 전 관문과 출력의 이력 참조 검사, 수렴·추가 라운드·결정 정지를 한곳에서 정한다.
"""Series state and every decision rule of the review loop (PLAN 5.9 steps 2 and 7, A22).

Series
    `series(records, log, base, head=..., is_ancestor=...)` keeps the receipts whose `base` is the series
    base, the correction attempts whose source receipt is one of them (plus attempts no receipt explains),
    their records in index order as the history, and the round-log lines of this base. History problems are
    the index problems, the attempt problems, and attempt commits that are not in the reviewed history.
Gate, before any paid call
    `gate(...)` raises `OAH_REVIEW_NEEDS_DECISION` for history problems (no decision overrides them), returns
    `"converged"` when the latest receipt converged at this head, raises `OAH_REVIEW_NEEDS_DECISION` for a
    pending decision stop or for P0/P1 findings of a changes-requested receipt without a finished
    correction attempt (both overridable with `--decision`), `OAH_REVIEW_ROUNDS_EXHAUSTED` at `max_rounds`
    unless `--extra-round`, and `OAH_REVIEW_ATTEMPTS_EXHAUSTED` once `round_attempts` calls of this round
    did not count. A decision stop is pending after a needs-decision receipt, or after an uncounted call
    that stopped for a decision, until a later call of the round starts with a decision.
Check, after the run
    `check(...)`: the canary line (`OAH_REVIEW_CANARY_MISSING`), the base/head and DB-impact echoes
    (`OAH_REVIEW_OUTPUT_INVALID`), the INTENT and AC lines (`OAH_INTENT_RETURN_INVALID`), then every class
    and prior reference against the series: an earlier round, its receipt digest, its finding and matching
    class ids. Broken references stop for a decision (`OAH_REVIEW_NEEDS_DECISION`); none of these rounds count.
Decide
    No P0/P1 finding ends the series (`converged`, P2/P3 become follow-ups). A P0/P1 finding in a class a
    finished correction attempt tried (by id, or by a new class whose normalized mechanism and rule equal
    that class's), one that needs a design or stored-format change, or one outside the task scope stops
    the series (`needs-decision`). Otherwise the series continues (`changes-requested`).
"""
from __future__ import annotations

import dataclasses
import re
from typing import Callable, Sequence

from oah import intent as oah_intent
from oah.review import ReviewError, canary
from oah.review.receipts import SHA_RE, Attempt, Entry, Records, attempts as read_attempts, history_digest
from oah.review.schema import BLOCKING, Output, OutputInvalid
from oah.review.settings import ReviewSettings

NEEDS_DECISION = "OAH_REVIEW_NEEDS_DECISION"
CONVERGED = "converged"


@dataclasses.dataclass(frozen=True)
class Series:
    task: str
    base: str
    receipts: tuple[Entry, ...]
    attempts: tuple[Attempt, ...]
    history: tuple[Entry, ...]
    problems: tuple[str, ...]
    next_number: int
    log: tuple[dict, ...]

    @property
    def series_round(self) -> int:
        return len(self.receipts) + 1

    @property
    def latest(self) -> Entry | None:
        return self.receipts[-1] if self.receipts else None

    def history_items(self) -> list[dict]:
        return [{"kind": entry.kind, "name": entry.name, "digest": entry.digest} for entry in self.history]

    def history_digest(self) -> str:
        return history_digest(self.history_items())

    def classes(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for entry in self.receipts:
            for item in entry.record.get("classes", []):
                found.setdefault(item["class_id"], item)
        return found

    def attempted(self) -> dict[str, list[tuple[str, str]]]:
        """Class id → (attempt id, outcome) of every finished correction attempt that tried it."""
        found: dict[str, list[tuple[str, str]]] = {}
        for attempt in self.attempts:
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
    mine = tuple(entry for entry in records.receipts() if entry.record.get("base") == base)
    digests = {entry.digest for entry in mine}
    known = {entry.digest for entry in records.receipts()}
    chosen = tuple(attempt for attempt in read_attempts(records)
                   if attempt.source_receipt_digest in digests or attempt.source_receipt_digest not in known)
    names = {entry.name for entry in mine} | {name for attempt in chosen for name in attempt.names}
    problems = list(records.problems)
    for attempt in chosen:
        problems += attempt.problems
        for label, commit in (("before_head", attempt.before_head), ("after_head", attempt.after_head)):
            if isinstance(commit, str) and SHA_RE.fullmatch(commit) and not is_ancestor(commit, head):
                problems.append(f"{attempt.attempt_id}: its {label} {commit} is not in the reviewed history (HEAD "
                                f"{head}); the branch was rewritten after the correction, so the evidence is ambiguous")
    return Series(task=records.task, base=base, receipts=mine, attempts=chosen,
                  history=tuple(entry for entry in records.entries if entry.name in names),
                  problems=tuple(problems), next_number=len(records.receipts()) + 1,
                  log=tuple(line for line in log if line.get("base") == base))


def _pending_stop(s: Series) -> str | None:
    latest = s.latest
    stop = None
    if latest is not None and latest.record.get("outcome") == "needs-decision":
        stop = f"{latest.name} stopped the series for a decision: {latest.record.get('decision_stop_reason')}"
    for line in s.round_lines():
        if line.get("event") == "start" and line.get("decision"):
            stop = None
        elif line.get("event") == "finish" and not line.get("counted") and line.get("code") == NEEDS_DECISION:
            stop = (f"the last reviewer call of round {s.series_round} did not count and stopped for a decision: "
                    f"{line.get('message', '')}")
    return stop


def _uncorrected(s: Series, latest: Entry) -> list[str]:
    covered = {finding for attempt in s.attempts if attempt.terminal
               and attempt.source_receipt_digest == latest.digest for finding in attempt.finding_ids}
    return [item["finding_id"] for item in latest.record.get("findings", [])
            if item.get("severity") in BLOCKING and item["finding_id"] not in covered]


def gate(s: Series, *, head: str, decision: str | None, extra_round: bool, settings: ReviewSettings) -> str | None:
    """Refuse before any paid call; `"converged"` means this head is already approved."""
    retry = f'oah review --task {s.task} --decision "<what you decided>"'
    if s.problems:
        raise ReviewError(NEEDS_DECISION, "the review history of this series is incomplete or was changed, so "
                          "recurrence cannot be judged and nothing was launched:\n- " + "\n- ".join(s.problems) +
                          "\nRestore or complete the records first (a --decision does not override this), then run "
                          f"oah review --task {s.task} again")
    latest = s.latest
    if latest is not None and latest.record.get("outcome") == CONVERGED and latest.record.get("head") == head:
        return CONVERGED
    stop = _pending_stop(s)
    if stop is not None and decision is None:
        raise ReviewError(NEEDS_DECISION, f"{stop}. Nothing was launched. Decide how to continue, then run {retry}; "
                          "the decision is recorded")
    if latest is not None and latest.record.get("outcome") == "changes-requested" and decision is None:
        missing = _uncorrected(s, latest)
        if missing:
            raise ReviewError(NEEDS_DECISION, f"{latest.name} requested changes, but no finished correction attempt "
                              f"is recorded for {', '.join(missing)}, so a recurrence could not be recognized and "
                              "nothing was launched. Record the corrections (oah fix records its own; manual "
                              f"corrections need the same records), or run {retry}")
    if settings.max_rounds is not None and len(s.receipts) >= settings.max_rounds and not extra_round:
        raise ReviewError("OAH_REVIEW_ROUNDS_EXHAUSTED", f"this series has {len(s.receipts)} counted rounds, the "
                          f"[review] max_rounds limit, so nothing was launched. Run oah review --task {s.task} "
                          "--extra-round to continue (it is recorded), or raise or remove max_rounds in "
                          ".oah/config.toml")
    used = sum(1 for line in s.round_lines() if line.get("event") == "start")
    if used >= settings.round_attempts:
        raise ReviewError("OAH_REVIEW_ATTEMPTS_EXHAUSTED", f"round {s.series_round} of this series already used "
                          f"{used} reviewer calls that did not count ([review] round_attempts = "
                          f"{settings.round_attempts}), so oah does not call the paid reviewer again. Read the last "
                          "run's stderr.log in the state folder's runs/ to find the cause and fix it, then raise "
                          "round_attempts in .oah/config.toml to allow another call")
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


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _tries(tried: list[tuple[str, str]]) -> str:
    word = "correction attempt" if len(tried) == 1 else "correction attempts"
    return f"{word} " + ", ".join(f"{attempt} ({outcome})" for attempt, outcome in tried)


def decide(output: Output, s: Series) -> Decision:
    blocking = output.blocking()
    if not blocking:
        return Decision(CONVERGED, None, tuple(finding.finding_id for finding in output.findings))
    attempted = s.attempted()
    classes = s.classes()
    twins = {(_normalized(classes[c]["mechanism"]), _normalized(classes[c]["rule"])): c for c in attempted if c in classes}
    new = {item.class_id: item for item in output.classes}
    reasons: list[str] = []
    for finding in blocking:
        if finding.class_id in attempted:
            reasons.append(f"{finding.finding_id} repeats class {finding.class_id}, which "
                           f"{_tries(attempted[finding.class_id])} already tried to fix")
        elif finding.class_id in new:
            twin = twins.get((_normalized(new[finding.class_id].mechanism), _normalized(new[finding.class_id].rule)))
            if twin is not None:
                reasons.append(f"{finding.finding_id} defines {finding.class_id} with the mechanism and rule of class "
                               f"{twin}, which {_tries(attempted[twin])} already tried to fix")
        if finding.fix_kind != "local":
            reasons.append(f"{finding.finding_id} needs a {finding.fix_kind} change")
        if not finding.in_scope:
            reasons.append(f"{finding.finding_id}'s fix lies outside the task scope")
    if reasons:
        return Decision("needs-decision", "; ".join(reasons), ())
    return Decision("changes-requested", None, ())
