from privaite.config.schema import CustomPatternConfig
from privaite.pii.recognizer_custom import CustomPatternRecognizer


def test_custom_pattern_match():
    patterns = [
        CustomPatternConfig(pattern=r"KD-\d{6}", entity_type="CUSTOMER_ID"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="fr")
    results = rec.analyze("Mon numéro client est KD-123456.", ["CUSTOMER_ID"], None)
    assert len(results) == 1
    text = "Mon numéro client est KD-123456."
    assert text[results[0].start : results[0].end] == "KD-123456"


def test_multiple_patterns():
    patterns = [
        CustomPatternConfig(pattern=r"KD-\d{6}", entity_type="CUSTOMER_ID"),
        CustomPatternConfig(pattern=r"REF-[A-Z]{3}-\d+", entity_type="REFERENCE"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="en")
    results = rec.analyze("Order KD-999888 ref REF-ABC-42", ["CUSTOMER_ID", "REFERENCE"], None)
    assert len(results) == 2


def test_no_match():
    patterns = [
        CustomPatternConfig(pattern=r"SECRET-\d+", entity_type="SECRET"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="fr")
    results = rec.analyze("Nothing to see here", ["SECRET"], None)
    assert len(results) == 0


def test_custom_score():
    patterns = [
        CustomPatternConfig(pattern=r"XX\d{4}", entity_type="CODE", score=0.75),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="fr")
    results = rec.analyze("Code XX1234", ["CODE"], None)
    assert results[0].score == 0.75


def test_named_group_spans_only_the_value():
    # A named group is how an operator says "the value is here". The span used
    # to be forced to the whole match (the group was hardcoded away), so the
    # field name and the "=" were anonymized with the secret and the
    # placeholder swallowed the label.
    patterns = [
        CustomPatternConfig(pattern=r"api_key=(?P<value>[A-Za-z0-9]+)", entity_type="SECRET"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="en")
    text = "line: api_key=Ab12Cd34 end"
    results = rec.analyze(text, ["SECRET"], None)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "Ab12Cd34"


def test_unnamed_group_keeps_the_whole_match_span():
    # Unchanged behaviour when nothing is named: a positional group is not a
    # value marker, so the whole match stays the span.
    patterns = [
        CustomPatternConfig(pattern=r"KD-(\d{6})", entity_type="CUSTOMER_ID"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="en")
    text = "ticket KD-123456 open"
    results = rec.analyze(text, ["CUSTOMER_ID"], None)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "KD-123456"


def test_value_group_wins_over_other_named_groups():
    # Several named groups must resolve deterministically: `value` is the
    # documented name for the span to anonymize.
    patterns = [
        CustomPatternConfig(pattern=r"(?P<field>token)=(?P<value>[A-Z0-9]+)", entity_type="SECRET"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="en")
    text = "token=ABC123"
    results = rec.analyze(text, ["SECRET"], None)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "ABC123"


def test_first_named_group_is_used_when_none_is_called_value():
    patterns = [
        CustomPatternConfig(pattern=r"ref\s+(?P<code>[A-Z]{3}\d+)", entity_type="REFERENCE"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="en")
    text = "see ref ABC42 please"
    results = rec.analyze(text, ["REFERENCE"], None)
    assert len(results) == 1
    assert text[results[0].start : results[0].end] == "ABC42"


def test_named_group_that_did_not_participate_yields_no_span():
    # An optional group that did not match reports (-1, -1); emitting that span
    # would anonymize the wrong characters.
    patterns = [
        CustomPatternConfig(pattern=r"pin(?P<value>\d+)?", entity_type="SECRET"),
    ]
    rec = CustomPatternRecognizer(patterns, supported_language="en")
    assert rec.analyze("pin unknown", ["SECRET"], None) == []
    text = "pin 4321 vs pin7788"
    results = rec.analyze(text, ["SECRET"], None)
    assert [text[r.start : r.end] for r in results] == ["7788"]
