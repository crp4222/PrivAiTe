from privaite.pii.entity import PIIEntity, merge_entities


def _entity(
    entity_type: str = "PERSON",
    text: str = "John",
    start: int = 0,
    end: int = 4,
    score: float = 0.9,
    source: str = "presidio",
) -> PIIEntity:
    return PIIEntity(
        entity_type=entity_type,
        text=text,
        start=start,
        end=end,
        score=score,
        source=source,
    )


def test_merge_no_overlap():
    entities = [
        _entity(start=0, end=4, text="John"),
        _entity(entity_type="EMAIL_ADDRESS", start=10, end=25, text="john@example.com"),
    ]
    result = merge_entities(entities)
    assert len(result) == 2
    assert result[0].start == 0
    assert result[1].start == 10


def test_merge_same_type_overlap():
    entities = [
        _entity(start=0, end=8, text="John Doe", score=0.8),
        _entity(start=5, end=12, text="Doe Smith", score=0.9, source="mlmodel"),
    ]
    result = merge_entities(entities)
    assert len(result) == 1
    assert result[0].score == 0.9
    # the merged span covers BOTH detections, never just the winner
    assert result[0].start == 0
    assert result[0].end == 12


def test_merge_different_type_overlap_highest_score():
    entities = [
        _entity(entity_type="PERSON", start=0, end=10, score=0.7),
        _entity(entity_type="LOCATION", start=5, end=15, score=0.95),
    ]
    result = merge_entities(entities, overlap_resolution="highest_score")
    assert len(result) == 1
    assert result[0].entity_type == "LOCATION"
    assert result[0].start == 0
    assert result[0].end == 15


def test_merge_overlap_never_unmasks_the_loser_remainder():
    # A long low-score address overlapped by a short high-score DATE must not
    # discard the address: the uncovered remainder would go to the provider raw.
    text = "42 rue Victor Hugo, 69002 Lyon"
    entities = [
        _entity(entity_type="LOCATION", start=0, end=30, score=0.6, text=text),
        _entity(entity_type="DATE_TIME", start=20, end=25, score=0.95, text="69002"),
    ]
    result = merge_entities(entities, source_text=text)
    assert len(result) == 1
    assert result[0].start == 0
    assert result[0].end == 30
    assert result[0].text == text


def test_merge_presidio_priority_falls_back_to_score():
    # Neither entity from presidio: presidio_priority must pick by score, not
    # arbitrarily keep the later-sorted one.
    entities = [
        _entity(entity_type="PERSON", start=0, end=10, score=0.9, source="onnx"),
        _entity(entity_type="LOCATION", start=5, end=15, score=0.5, source="mlmodel"),
    ]
    result = merge_entities(entities, overlap_resolution="presidio_priority")
    assert len(result) == 1
    assert result[0].entity_type == "PERSON"


def test_merge_different_type_overlap_longest_span():
    entities = [
        _entity(entity_type="PERSON", start=0, end=15, score=0.7, text="a" * 15),
        _entity(entity_type="LOCATION", start=5, end=12, score=0.95, text="b" * 7),
    ]
    result = merge_entities(entities, overlap_resolution="longest_span")
    assert len(result) == 1
    assert result[0].entity_type == "PERSON"


def test_merge_empty():
    assert merge_entities([]) == []


def test_merge_single():
    e = _entity()
    result = merge_entities([e])
    assert len(result) == 1
    assert result[0].text == "John"


def test_merge_adjacent_no_overlap():
    entities = [
        _entity(start=0, end=5, text="Hello"),
        _entity(start=5, end=10, text="World"),
    ]
    result = merge_entities(entities)
    assert len(result) == 2


def test_intersection_requires_two_distinct_sources():
    # two overlapping spans from the SAME detector (e.g. chunk-window duplicates)
    # must not self-confirm under intersection.
    same = [
        _entity(start=0, end=10, score=0.9, source="bert_ner"),
        _entity(start=2, end=12, score=0.8, source="bert_ner"),
    ]
    assert merge_entities(same, strategy="intersection") == []

    cross = [
        _entity(start=0, end=10, score=0.9, source="bert_ner"),
        _entity(start=2, end=12, score=0.8, source="presidio"),
    ]
    result = merge_entities(cross, strategy="intersection")
    assert len(result) == 1
    # both members survive as a UNION span: the longer remainder is not dropped
    assert result[0].start == 0
    assert result[0].end == 12


def test_intersection_with_single_detector_refused_at_startup():
    # intersection + <2 detectors would detect NOTHING and forward all PII raw;
    # the engine must refuse to initialize instead of failing silently open.
    # (With zero detectors the broader no-detector guard fires first; both are
    # startup refusals, either message is a correct outcome here.)
    import asyncio

    import pytest

    from privaite.config.schema import (
        DetectorsConfig,
        PIIConfig,
        PresidioDetectorConfig,
    )
    from privaite.pii.engine import PIIEngine

    config = PIIConfig(
        enabled=True,
        preset=None,
        merge_strategy="intersection",
        detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
    )
    engine = PIIEngine(config)
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError, match="intersection|no detector"):
            loop.run_until_complete(engine.initialize())
    finally:
        loop.close()
