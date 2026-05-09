from privaite.pii.tracker import PIITracker


def test_record_and_get():
    t = PIITracker()
    stats = t.record("session1", {"PERSON": 2, "EMAIL_ADDRESS": 1})
    assert stats.total_pii == 3
    assert stats.request_count == 1


def test_cumulative():
    t = PIITracker()
    t.record("s1", {"PERSON": 1})
    t.record("s1", {"PERSON": 2, "PHONE_NUMBER": 1})
    stats = t.get("s1")
    assert stats.total_pii == 4
    assert stats.pii_count["PERSON"] == 3
    assert stats.request_count == 2


def test_separate_sessions():
    t = PIITracker()
    t.record("s1", {"PERSON": 1})
    t.record("s2", {"EMAIL_ADDRESS": 5})
    assert t.get("s1").total_pii == 1
    assert t.get("s2").total_pii == 5


def test_unknown_session():
    t = PIITracker()
    assert t.get("nonexistent") is None


def test_empty_record():
    t = PIITracker()
    stats = t.record("s1", {})
    assert stats.total_pii == 0
    assert stats.request_count == 1
