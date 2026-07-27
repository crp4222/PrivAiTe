"""Regenerate llms-full.txt from the README and the docs pages.

llms-full.txt is a concatenation, so it silently goes stale whenever a page is
edited. Run this after any documentation change (and at every release):

    python scripts/gen_llms_full.py

`--check` exits non-zero instead of writing, for a gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "llms-full.txt"

HEADER = (
    "# PrivAiTe, full documentation\n"
    "\n"
    "> Concatenated project documentation for LLM consumption. The map with\n"
    "> one-line summaries per page is in llms.txt. Regenerated at each release."
)

# Order is the reading order of the docs site, README first.
SOURCES = (
    "README.md",
    "docs/detection.md",
    "docs/configuration.md",
    "docs/api.md",
    "docs/verify.md",
    "docs/comparison.md",
    "docs/gateway.md",
)


def _strip_frontmatter(text: str) -> str:
    """Drop the YAML frontmatter the docs pages carry for the site build."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n") :]


def render() -> str:
    chunks = [HEADER]
    for rel in SOURCES:
        body = _strip_frontmatter((REPO_ROOT / rel).read_text()).strip()
        chunks.append(f"---\n<!-- source: {rel} -->\n\n{body}")
    return "\n\n\n".join(chunks) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        if OUTPUT.read_text() != rendered:
            print("llms-full.txt is stale, run scripts/gen_llms_full.py", file=sys.stderr)
            return 1
        print("llms-full.txt is up to date")
        return 0
    OUTPUT.write_text(rendered)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
