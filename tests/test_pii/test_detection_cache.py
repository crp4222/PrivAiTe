"""Engine-level detection cache: correctness, boundedness, and the privacy
contract of the cache structure itself (spans only, never values)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading

import pytest

from privaite.config.schema import (
    DetectionCacheConfig,
    DetectorsConfig,
    PIIConfig,
    PresidioDetectorConfig,
)
from privaite.pii.cache import DetectionCache, entities_from_spans, spans_from_entities
from privaite.pii.engine import PIIBlockedError, PIIEngine, PIIProcessingError
from privaite.pii.entity import PIIEntity

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class RegexEmailDetector:
    """Deterministic stand-in detector so cache tests never depend on model
    weights. Counts calls: a cache hit is proven by the counter NOT moving."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "stub"

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        self.calls += 1
        return [
            PIIEntity("EMAIL_ADDRESS", m.group(0), m.start(), m.end(), 0.9, "stub")
            for m in EMAIL_RE.finditer(text)
        ]


def make_engine(
    *,
    cache_enabled: bool = True,
    max_entries: int = 4096,
    ttl_seconds: int = 1800,
    block: list[str] | None = None,
    detector: RegexEmailDetector | None = None,
) -> tuple[PIIEngine, RegexEmailDetector]:
    config = PIIConfig(
        preset=None,
        detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
        block_entities=list(block or []),
        detection_cache=DetectionCacheConfig(
            enabled=cache_enabled, max_entries=max_entries, ttl_seconds=ttl_seconds
        ),
    )
    engine = PIIEngine(config)
    det = detector or RegexEmailDetector()
    engine.detectors = [det]
    return engine, det


def _session_turns() -> list[list[dict]]:
    """An agent-style session: every turn resends the whole growing history,
    including tool calls whose JSON arguments carry PII."""
    base = [
        {"role": "system", "content": "You are a coding agent."},
        {
            "role": "user",
            "content": "Contact alice.durand@example.test about the incident report.",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps(
                            {"path": "notes.txt", "owner": "bob.martin@example.test"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "owner: bob.martin@example.test\nescalate to carol.smith@example.test",
        },
    ]
    turns = []
    history: list[dict] = []
    for i in range(4):
        history = history + base
        history = history + [
            {"role": "user", "content": f"turn {i}: also loop in dave.jones@example.test"}
        ]
        turns.append([dict(m) for m in history])
    return turns


async def _run_session(engine: PIIEngine, turns: list[list[dict]]) -> list[str]:
    out = []
    for turn in turns:
        scrubbed, _mapping = await engine.process_request(turn)
        out.append(json.dumps(scrubbed, ensure_ascii=False, sort_keys=True))
    return out


@pytest.mark.asyncio
async def test_scrub_output_byte_identical_cache_on_and_off():
    turns = _session_turns()
    engine_off, det_off = make_engine(cache_enabled=False)
    engine_on, det_on = make_engine(cache_enabled=True)

    out_off = await _run_session(engine_off, turns)
    out_on_cold = await _run_session(engine_on, turns)
    out_on_warm = await _run_session(engine_on, turns)

    assert out_on_cold == out_off
    assert out_on_warm == out_off
    # The cache actually did something: the disabled engine re-scanned every
    # leaf every turn, the enabled one scanned each unique leaf once.
    assert det_on.calls < det_off.calls
    assert engine_on._cache is not None and engine_on._cache.hits > 0


@pytest.mark.asyncio
async def test_cache_disabled_engine_has_no_cache_and_rescans_everything():
    engine, det = make_engine(cache_enabled=False)
    assert engine._cache is None
    msg = [{"role": "user", "content": "mail eve.adams@example.test"}]
    await engine.process_request(msg)
    await engine.process_request(msg)
    assert det.calls == 2


@pytest.mark.asyncio
async def test_blocked_entity_still_blocks_on_cache_hit():
    engine, det = make_engine(block=["EMAIL_ADDRESS"])
    msg = [{"role": "user", "content": "reach me at frank.lee@example.test"}]

    with pytest.raises(PIIBlockedError):
        await engine.process_request(msg)
    calls_after_first = det.calls

    # Second, identical request: detection is served from the cache (the
    # counter must not move), and the block gate must still fire. Fail closed
    # holds on the cached path.
    with pytest.raises(PIIBlockedError) as blocked:
        await engine.process_request(msg)
    assert det.calls == calls_after_first
    assert engine._cache is not None and engine._cache.hits > 0
    assert blocked.value.entity_types == ["EMAIL_ADDRESS"]
    # The safe error names types, never the value.
    assert "frank.lee" not in str(blocked.value)


def _walk_strings(obj: object, seen: set[int]) -> list[str]:
    """Collect every str and bytes reachable from obj (attributes, mappings,
    sequences), so the no-values assertion inspects the real stored structure
    rather than trusting its repr."""
    if id(obj) in seen:
        return []
    seen.add(id(obj))
    found: list[str] = []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, (bytes, bytearray)):
        return [obj.decode("utf-8", errors="ignore")]
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.extend(_walk_strings(k, seen))
            found.extend(_walk_strings(v, seen))
        return found
    if isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            found.extend(_walk_strings(item, seen))
        return found
    if hasattr(obj, "__dict__"):
        for v in vars(obj).values():
            found.extend(_walk_strings(v, seen))
    return found


@pytest.mark.asyncio
async def test_no_pii_value_and_no_anonymized_text_anywhere_in_cache():
    engine, _det = make_engine()
    secret_local_part = "grace.hopper"
    text = f"please email {secret_local_part}@example.test and keep this quiet"
    await engine.process_request([{"role": "user", "content": text}])

    assert engine._cache is not None
    assert len(engine._cache) > 0
    strings = _walk_strings(engine._cache, seen=set())
    joined = "\n".join(strings)
    # Neither the raw text, nor the matched value, nor the anonymized output
    # may live in the cache. Only span metadata is allowed.
    assert secret_local_part not in joined
    assert "example.test" not in joined
    assert "keep this quiet" not in joined
    assert "<EMAIL_ADDRESS_" not in joined
    # Every stored string is an entity type or a detector source.
    for _ts, spans in engine._cache._entries.values():
        for start, end, etype, score, source in spans:
            assert isinstance(start, int) and isinstance(end, int)
            assert etype == "EMAIL_ADDRESS"
            assert source == "stub"
            assert isinstance(score, float)


@pytest.mark.asyncio
async def test_ttl_expiry_forces_redetection():
    clock = {"now": 0.0}
    engine, det = make_engine()
    engine._cache = DetectionCache(max_entries=64, ttl_seconds=60, time_fn=lambda: clock["now"])
    msg = [{"role": "user", "content": "ping henry.ford@example.test"}]

    await engine.process_request(msg)
    clock["now"] = 59.0
    await engine.process_request(msg)
    assert det.calls == 1  # still cached

    clock["now"] = 61.0
    await engine.process_request(msg)
    assert det.calls == 2  # expired, detected again
    assert len(engine._cache) == 1  # the expired entry was dropped, then refilled


@pytest.mark.asyncio
async def test_lru_eviction_is_bounded_and_evicts_oldest():
    engine, det = make_engine(max_entries=2)
    texts = [f"user{i}.name@example.test" for i in range(3)]

    for t in texts:
        await engine.process_request([{"role": "user", "content": t}])
    assert engine._cache is not None
    assert len(engine._cache) == 2  # never exceeds the bound

    # texts[0] was evicted: scanning it again calls the detector.
    calls = det.calls
    await engine.process_request([{"role": "user", "content": texts[0]}])
    assert det.calls == calls + 1

    # texts[2] survived (most recent): served from cache.
    calls = det.calls
    await engine.process_request([{"role": "user", "content": texts[2]}])
    assert det.calls == calls


@pytest.mark.asyncio
async def test_detector_config_change_invalidates_via_fingerprint():
    engine, det = make_engine()
    msg = [{"role": "user", "content": "cc ivan.petrov@example.test"}]
    await engine.process_request(msg)
    await engine.process_request(msg)
    assert det.calls == 1

    # An in-place config mutation (detectors read config per call) must not
    # serve stale spans: the fingerprint changes, so the key changes.
    engine.config.detectors.presidio.score_threshold = 0.99
    await engine.process_request(msg)
    assert det.calls == 2


@pytest.mark.asyncio
async def test_language_is_part_of_the_key():
    engine, det = make_engine()
    text = "salut judy.garland@example.test"
    await engine._detect_all(text, "en")
    await engine._detect_all(text, "fr")
    assert det.calls == 2
    await engine._detect_all(text, "en")
    assert det.calls == 2


def test_salt_makes_keys_process_unique():
    fp = b"fingerprint"
    a = DetectionCache(max_entries=4, ttl_seconds=60)
    b = DetectionCache(max_entries=4, ttl_seconds=60)
    # Same text, same language, same fingerprint: still different keys, so the
    # key set is not a precomputable index of known documents.
    assert a.key("some text", "en", fp) != b.key("some text", "en", fp)
    # And within one cache, every component participates in the key.
    assert a.key("some text", "en", fp) != a.key("some text", "fr", fp)
    assert a.key("some text", "en", fp) != a.key("other text", "en", fp)
    assert a.key("some text", "en", fp) != a.key("some text", "en", b"other")


def test_cache_rejects_unbounded_configuration():
    with pytest.raises(ValueError):
        DetectionCache(max_entries=0, ttl_seconds=60)
    with pytest.raises(ValueError):
        DetectionCache(max_entries=4, ttl_seconds=0)


@pytest.mark.asyncio
async def test_detection_failure_is_never_cached():
    class FlakyDetector(RegexEmailDetector):
        async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
            if self.calls == 0:
                self.calls += 1
                raise RuntimeError("model exploded")
            return await super().detect(text, language)

    det = FlakyDetector()
    engine, _ = make_engine(detector=det)
    msg = [{"role": "user", "content": "kim.lee@example.test"}]

    with pytest.raises(PIIProcessingError):
        await engine.process_request(msg)
    assert engine._cache is not None and len(engine._cache) == 0

    # The failure was not replayed: the retry runs the detector and succeeds.
    scrubbed, _ = await engine.process_request(msg)
    assert det.calls == 2
    assert "kim.lee" not in json.dumps(scrubbed)


@pytest.mark.asyncio
async def test_concurrent_requests_are_safe_and_consistent():
    class SlowDetector(RegexEmailDetector):
        async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
            await asyncio.sleep(0.005)
            return await super().detect(text, language)

    det = SlowDetector()
    engine, _ = make_engine(detector=det, max_entries=64)
    texts = [f"person{i}@example.test in message {i % 5}" for i in range(5)]
    messages = [[{"role": "user", "content": texts[i % 5]}] for i in range(40)]

    results = await asyncio.gather(*(engine.process_request(m) for m in messages))
    outputs = [json.dumps(scrubbed, sort_keys=True) for scrubbed, _ in results]
    # Same input text, same scrubbed output, whichever task computed it.
    for i in range(40):
        assert outputs[i] == outputs[i % 5]
    assert engine._cache is not None and len(engine._cache) == 5


def test_cache_object_is_thread_safe_under_contention():
    cache = DetectionCache(max_entries=8, ttl_seconds=60)
    fp = b"fp"
    errors: list[BaseException] = []

    def hammer(worker: int) -> None:
        try:
            for i in range(500):
                key = cache.key(f"text-{worker}-{i % 16}", "en", fp)
                cache.put(key, ((0, 4, "EMAIL_ADDRESS", 0.9, "stub"),))
                cache.get(key)
        except BaseException as exc:  # pragma: no cover, only on a real race
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(cache) <= 8
    cache.clear()
    assert len(cache) == 0


def test_span_round_trip_rebuilds_against_live_text():
    text = "write to alice@example.test today"
    entity = PIIEntity("EMAIL_ADDRESS", "alice@example.test", 9, 27, 0.9, "stub")
    spans = spans_from_entities([entity])
    # The stored record carries no value.
    assert spans == ((9, 27, "EMAIL_ADDRESS", 0.9, "stub"),)
    rebuilt = entities_from_spans(spans, text)
    assert rebuilt == [entity]


CAPTURE_ENV = "PRIVAITE_CAPTURE_TAP"


@pytest.mark.skipif(
    CAPTURE_ENV not in os.environ,
    reason="set PRIVAITE_CAPTURE_TAP to a gateway tap .jsonl of captured request bodies",
)
@pytest.mark.asyncio
async def test_real_captured_session_byte_identical_cache_on_and_off():
    """Replay a real captured agent session (OpenAI Responses bodies recorded by
    the gateway tap) through the real scrub path with real detectors, and prove
    the scrubbed bodies are byte-identical with the cache off, cold, and warm."""
    from privaite.gateway.scrub import scrub_responses_request

    bodies = []
    with open(os.environ[CAPTURE_ENV], encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if record.get("kind") == "probe" or "body" not in record:
                continue
            bodies.append(json.loads(record["body"]))
    assert bodies, "capture file contains no request bodies"

    engine_off = PIIEngine(PIIConfig(preset="light"))
    await engine_off.initialize()
    engine_on = PIIEngine(
        PIIConfig(preset="light", detection_cache=DetectionCacheConfig(enabled=True))
    )
    engine_on.detectors = engine_off.detectors  # share the loaded models

    async def run(engine: PIIEngine) -> list[bytes]:
        out = []
        for body in bodies:
            scrubbed, _mapping = await scrub_responses_request(engine, body)
            out.append(json.dumps(scrubbed, ensure_ascii=False, sort_keys=True).encode())
        return out

    out_off = await run(engine_off)
    out_cold = await run(engine_on)
    out_warm = await run(engine_on)

    assert out_cold == out_off
    assert out_warm == out_off
    assert engine_on._cache is not None and engine_on._cache.hits > 0
