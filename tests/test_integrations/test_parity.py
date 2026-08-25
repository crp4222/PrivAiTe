"""The two in-process integrations each ship as one self-contained file (the Open
WebUI filter is pasted verbatim onto the hub), so the helpers they share cannot
be imported from a common module: they are copied. A fix landing in one copy and
not the other is the failure mode this pins. It has already happened once, to the
declared privaite floor, which stayed behind in the filter while the file's
content moved on.

The comparison is on the AST with docstrings stripped, so wording may differ
between copies but logic may not.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDRAIL = REPO_ROOT / "integrations" / "litellm" / "privaite_guardrail.py"
FILTER = REPO_ROOT / "integrations" / "openwebui" / "privaite_filter.py"

# Module-level helpers that exist, by design, as a copy in each integration.
# Adding a shared helper means adding it here, so its copies are held together.
SHARED_HELPERS = ("_restore_json_tree", "_restore_arguments")


def _logic(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # the docstring may differ, the logic may not
            return ast.dump(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{path.name} has no module-level function {name}")


@pytest.mark.parametrize("name", SHARED_HELPERS)
def test_shared_helper_is_identical_in_both_integrations(name: str) -> None:
    assert _logic(GUARDRAIL, name) == _logic(FILTER, name), (
        f"{name} differs between the LiteLLM guardrail and the Open WebUI filter: "
        "a change to a shared helper must land in both copies"
    )


def test_every_shared_helper_name_is_listed() -> None:
    """A helper defined at module level in both files is a copy whether or not
    it is listed above; an unlisted one would drift unnoticed."""

    def names(path: Path) -> set[str]:
        tree = ast.parse(path.read_text())
        return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    shared = names(GUARDRAIL) & names(FILTER)
    assert shared == set(SHARED_HELPERS), (
        f"module-level helpers shared by both integrations: {sorted(shared)}; "
        f"listed in SHARED_HELPERS: {sorted(SHARED_HELPERS)}"
    )
