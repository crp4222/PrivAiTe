"""Values an operator exempts from redaction (``pii.allow_list``).

A detector only sees shapes. ``chacha20-poly1305@openssh.com`` has the shape of
an e-mail address, and no model can tell it from one without knowing that the
SSH protocol names its algorithms that way. The operator knows; this is where
that knowledge goes: exact values, or regexes a detection must match in full,
optionally limited to some entity types.

What keeps it fail-closed:

* Full match only. A detection whose text merely contains an allowed value
  stays redacted, so an entry can never unmask the text around it.
* Every raw detection under a merged span must be allowed, each on its own
  type and text. Detection and merging run unchanged first, so an allowed
  e-mail never unmasks a name another detector found inside it, with ``union``
  or ``intersection`` merging alike. A span is never split: it is let through
  whole or kept whole.
* An entry without ``entity_types`` never applies to SECRET, nor to a type in
  ``block_entities``: a pattern written for hostnames cannot unmask a
  credential or weaken a hard policy gate by accident.
* A pattern that matches the empty string, or one of a few synthetic PII
  shapes in full, is refused at startup: that is a type-level switch dressed
  as a value-level exemption.
* A detection longer than ``MAX_ALLOWED_LENGTH`` is never exempted, which also
  bounds the work an operator regex does per detection.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from privaite.pii.entity import PIIEntity

if TYPE_CHECKING:
    from privaite.config.schema import AllowListEntry

MAX_ALLOWED_LENGTH = 1024

# Synthetic shapes (documentation ranges and example domains, no real data). A
# value-level exemption has no business matching any of them in full.
CANARIES = (
    "jane.doe@example.org",
    "Jane Doe",
    "+1 415 555 0132",
    "4111 1111 1111 1111",
    "DE89 3704 0044 0532 0130 00",
    "078-05-1120",
    "203.0.113.7",
    "1984-03-12",
    "https://example.org/profile/jane",
)

# Types an entry must name to touch: a credential, or a type the operator
# rejects outright. The engine adds its block_entities to this set.
ALWAYS_PROTECTED = frozenset({"SECRET"})


def check_entry(value: str | None, pattern: str | None, entity_types: list[str] | None) -> None:
    """Refuse an entry that cannot mean what the operator thinks it means.

    Raises ValueError. The message names the rule, never the offending text.
    """
    if (value is None) == (pattern is None):
        raise ValueError("an allow_list entry needs exactly one of value or pattern")
    if value is not None and not value:
        raise ValueError("an allow_list value must not be empty")
    if pattern is not None:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid allow_list pattern: {exc.msg}") from None
        if compiled.fullmatch(""):
            raise ValueError("an allow_list pattern must not match the empty string")
        if any(compiled.fullmatch(canary) for canary in CANARIES):
            raise ValueError(
                "an allow_list pattern matches a sample e-mail, name, phone, card, IBAN, "
                "SSN, IP address, date or URL in full; narrow it to the values you mean"
            )
    if entity_types is not None and not entity_types:
        raise ValueError("allow_list entity_types must name at least one type, or be omitted")


@dataclass(frozen=True)
class AllowRule:
    matches: Callable[[str], bool]
    entity_types: frozenset[str] | None

    def allows(self, entity_type: str, value: str, protected: frozenset[str]) -> bool:
        if self.entity_types is None:
            if entity_type in protected:
                return False
        elif entity_type not in self.entity_types:
            return False
        return len(value) <= MAX_ALLOWED_LENGTH and self.matches(value)


def _exact(expected: str) -> Callable[[str], bool]:
    def matches(value: str) -> bool:
        return value == expected

    return matches


def _full(pattern: str) -> Callable[[str], bool]:
    compiled = re.compile(pattern)

    def matches(value: str) -> bool:
        return compiled.fullmatch(value) is not None

    return matches


def compile_rules(entries: Iterable[AllowListEntry]) -> tuple[AllowRule, ...]:
    rules = []
    for entry in entries:
        matches = _exact(entry.value) if entry.value is not None else _full(entry.pattern or "")
        types = frozenset(entry.entity_types) if entry.entity_types is not None else None
        rules.append(AllowRule(matches, types))
    return tuple(rules)


def _sorted_and_disjoint(spans: Sequence[PIIEntity]) -> bool:
    return all(spans[i].end <= spans[i + 1].start for i in range(len(spans) - 1)) and all(
        spans[i].start <= spans[i + 1].start for i in range(len(spans) - 1)
    )


class AllowListVerdict(NamedTuple):
    redacted: list[PIIEntity]
    allowed: list[PIIEntity]
    # Raw detections no entry covers, under a span another entry did cover: the
    # reason a listed value is still redacted (a hostname seen inside an e-mail
    # address, a name seen inside a longer match). Reported by /v1/pii/inspect.
    not_allowed: list[PIIEntity]


def apply_allow_list(
    text: str,
    merged: Sequence[PIIEntity],
    raw: Iterable[PIIEntity],
    rules: Sequence[AllowRule],
    protected: frozenset[str] = ALWAYS_PROTECTED,
) -> AllowListVerdict:
    """Split the merged detections into the ones to redact and the ones let
    through, with the detections that stood in the way.

    ``raw`` are the detections the merge was built from. A merged span is let
    through only when at least one raw detection covers it and every raw
    detection overlapping it is allowed by some rule, on that detection's own
    type and text. With no rules, everything is redacted and nothing is spent.
    """
    if not rules or not merged:
        return AllowListVerdict(list(merged), [], [])
    verdict: list[bool | None] = [None] * len(merged)
    allowed_under: list[bool] = [False] * len(merged)
    refused: list[tuple[list[int], PIIEntity]] = []
    fast = _sorted_and_disjoint(merged)
    starts = [span.start for span in merged]
    ends = [span.end for span in merged]
    for detection in raw:
        if fast:
            # Merged spans are disjoint and ordered, so the ones a detection
            # overlaps form one index range: end > its start, start < its end.
            lo = bisect_right(ends, detection.start)
            hi = bisect_left(starts, detection.end)
            indexes: Iterable[int] = range(lo, hi)
        else:
            indexes = [
                i
                for i, span in enumerate(merged)
                if span.start < detection.end and detection.start < span.end
            ]
        touched = list(indexes)
        if not touched:
            continue
        value = text[detection.start : detection.end]
        ok = any(rule.allows(detection.entity_type, value, protected) for rule in rules)
        for i in touched:
            previous = verdict[i]
            verdict[i] = ok if previous is None else (previous and ok)
            allowed_under[i] = allowed_under[i] or ok
        if not ok:
            refused.append((touched, detection))
    kept: list[PIIEntity] = []
    allowed: list[PIIEntity] = []
    for span, let_through in zip(merged, verdict, strict=True):
        (allowed if let_through else kept).append(span)
    not_allowed = [d for touched, d in refused if any(allowed_under[i] for i in touched)]
    return AllowListVerdict(kept, allowed, not_allowed)
