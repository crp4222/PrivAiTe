from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts


class RegexSpanRecognizer(EntityRecognizer):
    """Base for recognizers that emit one RecognizerResult per regex match over a
    fixed list of patterns. Subclasses fill ``self._specs`` with
    (compiled, entity_type, score, group) tuples in __init__; ``group`` is the named
    span to report, or None for the whole match. Recognizers with per-match logic
    (trimming, normalization) implement their own ``analyze`` instead."""

    _specs: list[tuple[re.Pattern[str], str, float, str | None]]

    def load(self) -> None:
        pass

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        results = []
        for compiled, entity_type, score, group in self._specs:
            for match in compiled.finditer(text):
                start = match.start(group) if group else match.start()
                end = match.end(group) if group else match.end()
                if start < 0 or end <= start:
                    # An optional named group that did not participate reports
                    # (-1, -1): reporting it would anonymize the wrong
                    # characters. An empty span would anonymize nothing.
                    continue
                results.append(
                    RecognizerResult(
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        score=score,
                    )
                )
        return results
