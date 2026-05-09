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
