# 기능 항목 선택 — 기능 부류 필요마다 자격 있는 같은 부류 항목 2..8개를 공개 문서 발췌와 함께 capability-select/v1 Choice 로 고르고, 그 밖에는 설정 순서를 쓴다.
"""Entry selection for one capability-class need (DESIGN §7.1.1, capability-select/v1).

The candidates are the qualified entries of the need's class in inventory order: the child's own first (direct), then
the lead's (parent relay). At most OPTION_LIMIT become options o0..o7; the rest are `deferred(candidate-limit)`,
never rejected. Each option's only outgoing text is a verified public documentation slice the user linked to that
exact capability ID (`[capabilities] docs`); without one for every option the outcome is
`no-public-candidate-evidence` and the rule entry (the first candidate) stands. The record is
CapabilitySelectionAdviceV1; a selected option is applied only after the judge's recheck against the same option map
and task text. A class judgement never selects an entry by itself.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Mapping, Sequence

from oah.capabilities import RELAY_EFFECTS, digest
from oah.capabilities import inventory as inventories

OPTION_LIMIT = 8
POLICY = "oah-capability-select/v1"
POLICY_DIGEST = digest({"policy": POLICY, "options": OPTION_LIMIT})
OUTCOMES = ("selected", "no-match", "uncertain", "off", "error", "stale", "single-option", "no-candidate",
            "no-public-candidate-evidence")


EXCERPT_BYTES = 4000  # the start of a linked documentation file that one option may send


def linked_evidence(docs: Mapping[str, str], root) -> Callable[[str], object | None]:
    """evidence(capability_id) for `select`: the start of the verified public documentation file the user linked to
    that exact ID in `[capabilities] docs` (SOURCE:PATH of `oah sources`), or None for no link, an unregistered
    source, no verified snapshot or a file the snapshot does not hold. An ID is never matched by its name."""
    opened: list = []

    def evidence(capability: str):
        from oah.design.sources import SourceError, StateProblem, open_sources
        from oah.rerank import judge

        link = docs.get(capability)
        if link is None:
            return None
        source_id, _sep, path = link.partition(":")
        try:
            if not opened:
                opened.append(open_sources(root))
            sources = opened[0]
            head = sources.head(sources.spec(source_id))
            if head is None or head.verified is None:
                return None
            return judge.public_prefix(sources, head.verified.snapshot, path, EXCERPT_BYTES)
        except (SourceError, StateProblem, judge.JudgeError, OSError):
            return None

    return evidence


def candidates(cls: str, child: Mapping, parent: Mapping | None, now: dt.datetime) -> list[tuple[dict, str]]:
    """(entry, route) for every qualified entry of class `cls`: the child's own (direct), then the lead's whose
    effects a relay may carry (parent-relay). One ID appears once; the child's own copy wins."""
    out, seen = [], set()
    for entry in child.get("entries", ()):
        if entry.get("class") == cls and inventories.qualified(entry, now) and entry["id"] not in seen:
            seen.add(entry["id"])
            out.append((dict(entry), "direct"))
    for entry in (parent or {}).get("entries", ()):
        if (entry.get("class") == cls and inventories.qualified(entry, now) and entry["id"] not in seen
                and set(entry["permission"]["effects"]) <= set(RELAY_EFFECTS)):
            seen.add(entry["id"])
            out.append((dict(entry), "parent-relay"))
    return out


def _unapplied(status: str, reason: str | None) -> tuple[str, str]:
    """(outcome, reason) of advice that was not applied. When Jev answered (`used...`), no-match and uncertain are
    its own readings, a malformed answer is an error and any other apply reason (a changed slice, task, policy or
    provider) is stale. When nothing was answered, `unused(error:...)` is an error and every other reason (Jev off,
    task text not permitted, no key, budget, an open breaker) is off."""
    if status.startswith("used"):
        word = reason or "not-applied"
        if word in ("no-match", "uncertain"):
            return word, word
        return ("error" if word == "bad-response" else "stale"), word
    word = status[7:-1] if status.startswith("unused(") and status.endswith(")") else status or "not-used"
    return ("error" if word.startswith("error") else "off"), word


def select(need: Mapping, found: Sequence[tuple[Mapping, str]], *, prep: str, inventory_digest: str,
           evidence: Callable[[str], object | None], advise: Callable[[dict], dict] | None
           ) -> tuple[dict, tuple[Mapping, str] | None, list[str]]:
    """(CapabilitySelectionAdviceV1, the chosen (entry, route) or None, deferred capability IDs) for one class need.

    `evidence(capability_id)` returns the PublicSnapshotField the user linked to that ID, or None. `advise(request)`
    gets {"class", "options": [CapabilityOptionV1...], "fields": {option: field}, "option_map_digest"} and returns
    {"status", "applied", "choice", "reason", "judgement_ref"}; it is never called with fewer than two options, and
    `applied` is true only after the judge's recheck. A class need never refuses: every outcome but `selected` keeps
    the rule entry (the first candidate), and `no-candidate` selects nothing."""
    shortlist, deferred = list(found[:OPTION_LIMIT]), [entry["id"] for entry, _route in found[OPTION_LIMIT:]]
    rule = shortlist[0] if shortlist else None
    record = {"version": 1, "prep_id": prep, "need_id": need["id"], "class": need["class"], "option_map_digest": None,
              "inventory_digest": inventory_digest, "policy_digest": POLICY_DIGEST, "judgement_ref": None,
              "outcome": None, "selected_option": None, "selected_entry_digest": None,
              "rule_entry_digest": rule[0]["entry_digest"] if rule else None, "reason": None, "applied": False}

    def done(outcome: str, reason: str | None, pick: tuple[Mapping, str] | None) -> tuple:
        record.update(outcome=outcome, reason=reason)
        return dict(record, id=digest(record)), pick, deferred

    if not shortlist:
        return done("no-candidate", "no-qualified-entry", None)
    if len(shortlist) == 1:
        return done("single-option", None, rule)
    fields, options = {}, []
    for index, (entry, _route) in enumerate(shortlist):
        field = evidence(entry["id"])
        if field is None:
            return done("no-public-candidate-evidence", f"no linked public documentation for option o{index}", rule)
        key = f"o{index}"
        fields[key] = field
        options.append({"option": key, "need_id": need["id"], "capability_id": entry["id"],
                        "entry_digest": entry["entry_digest"], "schema_digest": entry["schema"]["sha256"],
                        "target_digest": entry["target_digest"], "permission_digest": digest(entry["permission"]),
                        "public_evidence": {"snapshot_ref": field.snapshot_ref,
                                            "path_digest": digest(field.path_or_field),
                                            "slice_digest": field.bytes_digest}})
    record["option_map_digest"] = digest(options)
    if advise is None:
        return done("off", "judgement-off", rule)
    answer = advise({"class": need["class"], "options": options, "fields": fields,
                     "option_map_digest": record["option_map_digest"]})
    record["judgement_ref"] = answer.get("judgement_ref")
    by_option = {item["option"]: (shortlist[index], item) for index, item in enumerate(options)}
    if answer.get("applied") is True:
        if answer.get("choice") not in by_option:
            return done("error", "choice-outside-options", rule)
        pick, item = by_option[answer["choice"]]
        record.update(selected_option=answer["choice"], selected_entry_digest=item["entry_digest"], applied=True)
        return done("selected", None, pick)
    return done(*_unapplied(str(answer.get("status") or ""), answer.get("reason")), rule)
