"""Pins for the CI workflows that are a safety net rather than a convenience.

Dependabot is deliberately muzzled here, so the weekly pip-audit run is the real
supply-chain signal: every `--ignore-vuln` entry silences part of it, and one
that outlives its justification turns a live finding into silence.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT = (REPO_ROOT / ".github" / "workflows" / "audit.yml").read_text()


def _ignored_advisories() -> list[str]:
    return re.findall(r"--ignore-vuln\s+(\S+)", AUDIT)


def test_dependency_audit_actually_runs_pip_audit() -> None:
    """The step must keep auditing the resolved environment; a red run is fixed
    by raising a floor, never by weakening the command."""
    assert "pip-audit --skip-editable" in AUDIT


def test_the_cryptography_advisory_is_no_longer_ignored() -> None:
    """GHSA-537c-gmf6-5ccf was ignored only because presidio-anonymizer capped
    cryptography<47, so no floor of ours could reach the fixed 48.0.1.
    presidio-anonymizer 2.2.364 requires cryptography>=48.0.1, so the freshest
    resolution the pyproject floors allow now reaches the fix: re-adding the
    ignore would hide a real finding instead of an unreachable one."""
    assert "GHSA-537c-gmf6-5ccf" not in _ignored_advisories()


def test_every_ignored_advisory_carries_a_justification() -> None:
    """The workflow's own rule: one justification comment per entry, naming the
    advisory, so the next reader can tell whether it is still unreachable."""
    comments = "\n".join(line for line in AUDIT.splitlines() if line.lstrip().startswith("#"))
    for advisory in _ignored_advisories():
        assert advisory in comments, f"{advisory} is ignored with no justification comment"


DOCKER_PUBLISH = (REPO_ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()


def test_the_image_is_smoke_tested_before_it_is_pushed() -> None:
    """The 0.7.3 to 0.8.2 images failed every request with tools while every
    test was green (#45): only a request through the built image sees that."""
    smoke = DOCKER_PUBLISH.index("privaite:smoke - < scripts/smoke_image.py")
    push = DOCKER_PUBLISH.index("push: true")
    assert smoke < push
    assert "load: true" in DOCKER_PUBLISH[:smoke]
