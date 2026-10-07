"""Refine built-in detections only where surrounding syntax is recognizable.

Never strip punctuation that can belong to a secret, choose the shortest
detector span, exempt path contents, or override an operator's custom regex
span. What is given back is structure by construction: a wrapper, an
upper-case name assigned at the start of a line, the separators of a URI
userinfo. Run before the union merge so a padded detector span cannot widen its
correctly bounded neighbour.
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
# What can stand before an assignment at the start of a line. An agent rarely
# sends the bare file: a read tool numbers the lines ("12:", "12<tab>",
# "12\u2192", "00012|"), a diff marks them, a compose file lists them.
ENV_LINE_PREFIX = (
    r"[ \t]*(?:[0-9]+[:|\t\u2192][ \t]*)?(?:[+-][ \t]*)?"
    r"(?:(?:export|env|ENV|ARG)[ \t]+)?"
)
# An upper-case name assigned at the start of a line (.env files, shell
# exports): the name is structure, never a value. Lower-case and mixed-case
# names stay ambiguous ("user=tag@example.com" is also one valid address) and
# are left alone. The lookahead keeps base32 padding ("SEED====") out.
_ENV_ASSIGNMENT = re.compile(ENV_LINE_PREFIX + r"(?P<key>[A-Z][A-Z0-9_]+)[ \t]*=[ \t]*(?=[^\s=])")
# The types that cannot be such a name on their own. A person or a place can
# (MARIE_DUPONT=admin), so those are never touched.
_ENV_VALUE_TYPES = frozenset({"EMAIL_ADDRESS", "SECRET", "URL"})
_URI_AUTHORITY = re.compile(r"[a-z][a-z0-9+.-]{0,31}://(?P<authority>[^\s/?#\"'<>]*)", re.I)


def _unescaped(text: str, index: int) -> bool:
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        cursor -= 1
    return (index - cursor - 1) % 2 == 0


def _line(text: str, index: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return start, len(text) if end < 0 else end


def _env_value_start(text: str, index: int) -> int | None:
    """Where the value starts, when this line assigns an upper-case name.

    A long single word with a digit ("AKIADEMO0000EXAMPLE=enabled") can be a
    token written before an "=": that line is not treated as an assignment.
    """
    line_start, line_end = _line(text, index)
    match = _ENV_ASSIGNMENT.match(text, line_start, line_end)
    if not match:
        return None
    key = match["key"]
    if len(key) >= 16 and "_" not in key and any(c.isdigit() for c in key):
        return None
    return match.end()


def _password_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """A secret that starts in the userinfo of a URI, without its separators.

    The model takes the colon before the password and runs on into the host
    (":pass@db.internal:5432/app"). The password starts after the first colon
    of the userinfo and stops at the last "@" of the authority. A tail with a
    query or a fragment is kept: it can carry another credential
    ("?sslpassword=...").
    """
    line_start, line_end = _line(text, start)
    for uri in _URI_AUTHORITY.finditer(text, line_start, line_end):
        first, last = uri.span("authority")
        at = text.rfind("@", first, last)
        if not first <= start <= at:
            continue
        if start == at:
            return min(start + 1, end), end
        if start == text.find(":", first, at) and start + 1 < end:
            start += 1
        if at < end and not re.search(r"[?#]", text[at:end]):
            end = at
        break
    return start, end


def refine_spans(text: str, entity: PIIEntity) -> list[PIIEntity]:
    """refine_boundary for a model span, which can also be empty, run across
    lines, or sit inside a name.

    On a .env file the model returns fragments of the variable names, spans
    that start mid-name and spans that run into the next line. A span is cut
    where the next line assigns an upper-case name, a piece that stays inside
    such a name is dropped, and a piece that starts there is cut to the value.
    No character of a value is ever uncovered.
    """
    if entity.start == entity.end:
        return []
    if entity.entity_type not in _ENV_VALUE_TYPES or not (
        0 <= entity.start < entity.end <= len(text)
    ):
        return [refine_boundary(text, entity)]

    pieces: list[tuple[int, int]] = []
    start = entity.start
    for index in range(entity.start, entity.end - 1):
        if text[index] == "\n" and _env_value_start(text, index + 1) is not None:
            pieces.append((start, index))
            start = index + 1
    pieces.append((start, entity.end))

    refined: list[PIIEntity] = []
    for start, end in pieces:
        while end > start and text[end - 1] in "\r\n":
            end -= 1
        if end <= start:
            continue
        value_start = _env_value_start(text, start)
        if value_start is not None and end <= value_start:
            continue
        if entity.entity_type == "SECRET":
            password_start, password_end = _password_bounds(text, start, end)
            if password_start >= password_end:
                continue
        piece = replace(entity, start=start, end=end, text=text[start:end])
        refined.append(refine_boundary(text, piece))
    return refined


def refine_boundary(text: str, entity: PIIEntity) -> PIIEntity:
    if entity.entity_type not in _DELIMITED_TYPES and entity.entity_type != "SECRET":
        return entity
    start, end = entity.start, entity.end
    if not 0 <= start < end <= len(text):
        return entity

    line_start, line_end = _line(text, start)
    if "\n" in text[start:end]:
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = len(text)
    line = text[line_start:line_end]

    if entity.entity_type in _ENV_VALUE_TYPES:
        value_start = _env_value_start(text, start)
        if value_start is not None and start < value_start < end:
            start = value_start

    if entity.entity_type == "SECRET":
        start, end = _password_bounds(text, start, end)
        if start >= end or (start, end) == (entity.start, entity.end):
            return entity
        return replace(entity, start=start, end=end, text=text[start:end])

    if entity.entity_type == "EMAIL_ADDRESS":
        # An address never ends with these; the model takes them from the
        # sentence or from a closing delimiter it then hides ("...com>,").
        while end - 1 > start and text[end - 1] in ",;.":
            end -= 1

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
