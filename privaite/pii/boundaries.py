"""Refine built-in detections only where surrounding syntax is recognizable.

Never strip punctuation from secrets, choose the shortest detector span, exempt
path contents, or override an operator's custom regex span. Run before the union
merge so a padded detector span cannot widen its correctly bounded neighbour.
"""

from __future__ import annotations

import re
from dataclasses import replace

from privaite.pii.entity import PIIEntity

_EMAIL_FIELD = re.compile(r"(?:EMAIL|EMAIL_ADDRESS|[A-Z_][A-Z0-9_]*_EMAIL(?:_ADDRESS)?)\Z", re.I)
_ASSIGNMENT = re.compile(
    r"(?:[ \t]*[0-9]+:[ \t]*)?[ \t]*(?:export[ \t]+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
)
_PATH = re.compile(r"<path>([^<>\r\n]+)</path>\Z")
_DELIMITED_TYPES = frozenset({"EMAIL_ADDRESS", "PHONE_NUMBER", "URL"})


def _unescaped(text: str, index: int) -> bool:
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        cursor -= 1
    return (index - cursor - 1) % 2 == 0


def refine_boundary(text: str, entity: PIIEntity) -> PIIEntity:
    if entity.entity_type not in _DELIMITED_TYPES:
        return entity
    start, end = entity.start, entity.end
    if not 0 <= start < end <= len(text):
        return entity

    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]

    if entity.entity_type == "EMAIL_ADDRESS":
        assignment = _ASSIGNMENT.match(line)
        if assignment and _EMAIL_FIELD.fullmatch(assignment["key"]):
            value_start = line_start + assignment.end()
            key_start = line_start + assignment.start("key")
            if key_start <= start < value_start < end and "@" in text[value_start:end]:
                start = value_start

    if entity.entity_type == "URL":
        path = _PATH.fullmatch(line)
        if path:
            inner_start, inner_end = (line_start + i for i in path.span(1))
            # Preserve the wrapper only, even if just part of its path is PII.
            start, end = max(start, inner_start), min(end, inner_end)

    for opening, closing in (('"', '"'), ("'", "'"), ("<", ">")):
        left = start if text[start : start + 1] == opening else start - 1
        right = end - 1 if text[end - 1 : end] == closing else end
        if (
            0 <= left < right < len(text)
            and text[left] == opening
            and text[right] == closing
            and _unescaped(text, left)
            and _unescaped(text, right)
            and "\n" not in text[left:right]
        ):
            start, end = max(start, left + 1), min(end, right)

    if start >= end or (start, end) == (entity.start, entity.end):
        return entity
    return replace(entity, start=start, end=end, text=text[start:end])
