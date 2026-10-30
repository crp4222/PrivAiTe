"""Presidio on blank spaCy pipelines (`nlp_engine: blank`, the ultrafast
preset): no spaCy model is loaded, the pattern recognizers and PrivAiTe's own
recognizers still fire, and spaCy's named entities are gone (names without a
cue are then left to the ML detector)."""

from __future__ import annotations

import pytest

from privaite.config.schema import PresidioDetectorConfig
from privaite.pii.detector_presidio import PresidioDetector, blank_nlp_engine


def test_blank_engine_loads_no_model_and_lemmatizes_to_lower_case():
    engine = blank_nlp_engine(["fr", "en"])

    assert sorted(engine.nlp) == ["en", "fr"]
    for nlp in engine.nlp.values():
        assert nlp.pipe_names == ["privaite_lower_lemma"]
    artifacts = engine.process_text("Contact Marie Dupont", "en")
    assert [token.lemma_ for token in artifacts.tokens] == ["contact", "marie", "dupont"]
    assert list(artifacts.entities) == []


def test_blank_engine_can_be_built_twice():
    blank_nlp_engine(["en"])
    blank_nlp_engine(["en"])


async def _detector(monkeypatch, nlp_engine: str) -> PresidioDetector:
    if nlp_engine == "blank":

        def no_models(*args, **kwargs):
            raise AssertionError("a spaCy model was requested")

        import presidio_analyzer.nlp_engine as nlp_engines

        monkeypatch.setattr(nlp_engines, "NlpEngineProvider", no_models)
    detector = PresidioDetector(
        PresidioDetectorConfig(languages=["fr", "en"], nlp_engine=nlp_engine)
    )
    await detector.initialize()
    return detector


@pytest.mark.asyncio
async def test_pattern_and_privaite_recognizers_still_fire_without_spacy(monkeypatch):
    detector = await _detector(monkeypatch, "blank")
    text = "Je m'appelle Marie Dupont, écrivez à marie.dupont@example.fr"

    found = {(e.entity_type, e.text) for e in await detector.detect(text, "fr")}

    assert ("PERSON", "Marie Dupont") in found
    assert ("EMAIL_ADDRESS", "marie.dupont@example.fr") in found


@pytest.mark.asyncio
async def test_a_name_with_no_cue_needs_an_ml_detector_without_spacy(monkeypatch):
    """The trade-off of the blank engine, stated: spaCy's NER found this name."""
    detector = await _detector(monkeypatch, "blank")

    found = await detector.detect("Yesterday Marie Dupont signed the lease.", "en")

    assert not [e for e in found if e.entity_type == "PERSON"]


def test_spacy_stays_the_default_engine():
    assert PresidioDetectorConfig().nlp_engine == "spacy"
