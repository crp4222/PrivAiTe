from __future__ import annotations

import re

from privaite.config.schema import CustomPatternConfig
from privaite.pii.recognizer_base import RegexSpanRecognizer


class CustomPatternRecognizer(RegexSpanRecognizer):
    def __init__(
        self,
        patterns: list[CustomPatternConfig],
        supported_language: str = "fr",
    ) -> None:
        super().__init__(
            supported_entities=list({p.entity_type for p in patterns}),
            supported_language=supported_language,
            name="CustomPatternRecognizer",
        )
        self._specs = [
            (re.compile(p.pattern, re.IGNORECASE | re.UNICODE), p.entity_type, p.score, None)
            for p in patterns
        ]
