"""Pins for the generated documentation artefacts.

`llms-full.txt` is a concatenation of the README and the docs pages, so it goes
stale the moment a page is edited: it once shipped three releases behind. These
tests turn that into a failing gate instead of a silent drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen_llms_full.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_llms_full", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_llms_full_matches_the_sources_it_concatenates() -> None:
    generator = _load_generator()
    assert generator.OUTPUT.read_text() == generator.render(), (
        "llms-full.txt is stale, run scripts/gen_llms_full.py"
    )


def test_llms_txt_and_its_docs_copy_are_identical() -> None:
    root_copy = (REPO_ROOT / "llms.txt").read_text()
    docs_copy = (REPO_ROOT / "docs" / "llms.txt").read_text()
    assert root_copy == docs_copy


@pytest.mark.parametrize("rel", ["README.md", "docs/detection.md", "docs/gateway.md"])
def test_the_miss_mechanism_is_stated_the_same_way_everywhere(rel: str) -> None:
    """The 2/24 miss is the project's only known live failure and its published
    explanation was measurably wrong once. Keep the corrected mechanism, and the
    claim that it is not gateway-specific, present wherever it is described."""
    text = (REPO_ROOT / rel).read_text()
    assert "preceding line of log-shaped context" in text
    # The superseded explanation must not come back.
    assert "full-log scale" not in text
    assert "only the full 69 KB log" not in text


def test_detection_docs_state_the_cross_surface_reach_and_the_measurements() -> None:
    text = (REPO_ROOT / "docs" / "detection.md").read_text()
    assert "order dependent" in text
    assert "4 of 5 and 3 of 5" in text
    for surface in ("OpenAI-compatible proxy", "Open WebUI filter", "LiteLLM guardrail"):
        assert surface in text


def test_readme_links_are_absolute() -> None:
    """The README is rendered on PyPI and Docker Hub, where a relative link is
    dead. Every link must be absolute, in-page anchors included."""
    import re

    text = (REPO_ROOT / "README.md").read_text()
    relative = [
        target
        for target in re.findall(r"\]\(([^)]+)\)", text)
        if not target.startswith(("http://", "https://", "mailto:"))
    ]
    assert relative == []


def test_openwebui_filter_floor_matches_the_shipped_version() -> None:
    """An Open WebUI environment on an older privaite is never upgraded, so the
    declared floor has to track the release."""
    from privaite import __version__

    filter_source = (REPO_ROOT / "integrations" / "openwebui" / "privaite_filter.py").read_text()
    readme = (REPO_ROOT / "integrations" / "openwebui" / "README.md").read_text()
    assert f"requirements: privaite>={__version__}" in filter_source
    assert f"privaite>={__version__}" in readme
