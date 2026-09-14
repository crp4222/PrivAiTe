"""Names of the recognizers PrivAiTe registers on top of Presidio's own.

Kept in a module of its own, free of any presidio or model import, so the
config schema can validate `disabled_recognizers` at load time without
building an analyzer. `tests/test_pii/test_recognizers.py` pins it against
what `build_recognizers` actually returns, so it cannot drift.
"""

from __future__ import annotations

BUILTIN_RECOGNIZER_NAMES = frozenset(
    {
        "ContextualNameRecognizer",
        "FrenchDateRecognizer",
        "ContextualLocationRecognizer",
        "StructuredSecretRecognizer",
    }
)
