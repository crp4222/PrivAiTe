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
    assert result[0].text == "Doe Smith"


def test_merge_different_type_overlap_highest_score():
    entities = [
        _entity(entity_type="PERSON", start=0, end=10, score=0.7),
        _entity(entity_type="LOCATION", start=5, end=15, score=0.95),
    ]
    result = merge_entities(entities, overlap_resolution="highest_score")
    assert len(result) == 1
    assert result[0].entity_type == "LOCATION"


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
