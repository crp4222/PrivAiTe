"""The Docker base image is pinned by digest and refreshed by hand at release.

Dependabot was meant to keep the digest current and did not: it closed its only
proposal as "up-to-date", and the 0.7.2 image shipped on a June base, three
months of Debian security fixes behind. The digest is now checked at every
release, and the date of that check sits next to the FROM line.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A release may ship a base checked up to this long before its date.
MAX_AGE_AT_RELEASE = timedelta(days=14)


def _base() -> tuple[str, date]:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    match = re.search(
        r"^# Base image checked on (\d{4}-\d{2}-\d{2})\b.*\n"
        r"FROM (python:[\w.-]+@sha256:[0-9a-f]{64})$",
        dockerfile,
        re.M,
    )
    assert match, "the FROM line must be digest-pinned, under '# Base image checked on YYYY-MM-DD'"
    return match[2], date.fromisoformat(match[1])


def _latest_release() -> date:
    match = re.search(
        r"^## \[\d+\.\d+\.\d+\] - (\d{4}-\d{2}-\d{2})$",
        (REPO_ROOT / "CHANGELOG.md").read_text(),
        re.M,
    )
    assert match, "no dated release in CHANGELOG.md"
    return date.fromisoformat(match[1])


def test_the_base_image_is_pinned_by_digest() -> None:
    image, _checked = _base()
    assert image.startswith("python:3.13-slim@sha256:")


def test_the_base_digest_was_checked_for_the_latest_release() -> None:
    """Dating a release in the changelog makes this fail until the base digest
    is checked again (docker buildx imagetools inspect python:3.13-slim) and the
    date above the FROM line moved. Between releases nothing changes, so the
    test never fails because time passed."""
    _image, checked = _base()
    released = _latest_release()
    assert checked >= released - MAX_AGE_AT_RELEASE, (
        f"the base digest was last checked on {checked}, more than "
        f"{MAX_AGE_AT_RELEASE.days} days before the {released} release"
    )
