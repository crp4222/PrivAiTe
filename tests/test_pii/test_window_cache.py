"""Request-local reuse: identical predictions, live offsets, isolation and cleanup."""

from __future__ import annotations

import asyncio
import threading

import pytest

from privaite.config.schema import PIIConfig
from privaite.pii.engine import PIIBlockedError, PIIEngine, PIIProcessingError
from privaite.pii.window_cache import WindowCache, current_window_cache, inference_request
from tests.test_pii.test_detector_onnx import _fake_onnx_detector


def counting_detector(enabled=True):
    detector = _fake_onnx_detector(max_length=16)
    detector.config.deduplicate_windows = enabled
    session = detector._session
    original = session.run
    session.calls = 0

    def run(*args):
        session.calls += 1
        return original(*args)

    session.run = run
    return detector


def engine_with(detector):
    engine = PIIEngine(PIIConfig())
    engine.detectors = [detector]
    return engine


@pytest.mark.asyncio
async def test_repeated_windows_at_different_offsets_match_uncached_output():
    cached, uncached = counting_detector(), counting_detector(False)
    body = "a" * 12 + "SECRET" + "b" * 40
    messages = [{"role": "tool", "content": text} for text in (body, "z" * 14 + body)]
    a, am = await engine_with(cached).process_request(messages)
    b, bm = await engine_with(uncached).process_request(messages)
    assert a == b
    assert am.get_all_fakes() == bm.get_all_fakes()
    assert "SECRET" not in a[0]["content"].replace("<SECRET_1>", "")
    assert cached._session.calls < uncached._session.calls
    assert current_window_cache() is None


@pytest.mark.asyncio
async def test_repeated_requests_do_not_share_predictions():
    detector = counting_detector()
    engine = engine_with(detector)
    messages = [{"role": "user", "content": "aaa SECRET bbb"}]
    await engine.process_request(messages)
    first = detector._session.calls
    await engine.process_request(messages)
    assert detector._session.calls == 2 * first


@pytest.mark.asyncio
async def test_document_walker_and_auxiliary_values_share_nested_scope():
    detector = counting_detector()
    engine = engine_with(detector)

    @inference_request
    async def request():
        _, mapping = await engine.scrub_document(["SECRET", "SECRET"])
        await engine.process_request_value(["SECRET", "SECRET"], mapping)

    await request()
    assert detector._session.calls == 1


@pytest.mark.asyncio
async def test_policy_still_blocks_when_predictions_are_reused():
    detector = counting_detector()
    engine = engine_with(detector)

    @inference_request
    async def request():
        await engine.process_request([{"role": "user", "content": "SECRET"}])
        engine._blocked.add("SECRET")
        with pytest.raises(PIIBlockedError):
            await engine.process_request([{"role": "user", "content": "SECRET"}])

    await request()
    assert detector._session.calls == 1
    assert current_window_cache() is None


@pytest.mark.asyncio
async def test_failure_closes_scope_and_cannot_turn_into_a_cache_hit(caplog):
    detector = counting_detector()
    engine = engine_with(detector)
    original = detector._session.run

    def broken(*args):
        raise RuntimeError("sensitive-error-content")

    detector._session.run = broken
    with pytest.raises(PIIProcessingError):
        await engine.process_request([{"role": "user", "content": "SECRET"}])
    assert current_window_cache() is None
    assert "sensitive-error-content" not in caplog.text
    detector._session.run = original
    await engine.process_request([{"role": "user", "content": "SECRET"}])
    assert detector._session.calls == 1


@pytest.mark.asyncio
async def test_concurrent_scopes_are_isolated_and_nested_calls_share():
    seen = []

    @inference_request
    async def nested():
        return current_window_cache()

    @inference_request
    async def request():
        cache = current_window_cache()
        assert await nested() is cache
        seen.append(cache)
        await asyncio.sleep(0)
        assert current_window_cache() is cache

    await asyncio.gather(request(), request())
    assert seen[0] is not seen[1]
    assert all(c._closed and not c._entries for c in seen)


@pytest.mark.asyncio
async def test_cancelled_request_drops_entries_and_rejects_late_thread_writes():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    held = []

    @inference_request
    async def request():
        cache = current_window_cache()
        held.append(cache)

        def worker():
            assert current_window_cache() is cache
            cache.put(b"first", ((0,), (0.9,)))
            entered.set()
            release.wait(timeout=5)
            cache.put(b"late", ((1,), (0.8,)))
            finished.set()

        await asyncio.to_thread(worker)

    task = asyncio.create_task(request())
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    assert held[0]._closed and not held[0]._entries
    assert current_window_cache() is None


def test_keys_cover_session_mask_shape_and_private_salt():
    cache = WindowCache()
    session, other = object(), object()
    inputs = [("input_ids", (1, 2), b"secret-input"), ("attention_mask", (1, 2), b"11")]
    key = cache.key(session, inputs)
    assert key != cache.key(other, inputs)
    assert key != WindowCache().key(session, inputs)
    assert key != cache.key(session, inputs[:1] + [("attention_mask", (1, 2), b"10")])
    assert key != cache.key(session, [("input_ids", (2, 1), b"secret-input"), inputs[1]])
    cache.put(key, ((0, 2), (0.99, 0.95)))
    assert b"secret-input" not in repr(cache.__dict__).encode()
    assert cache.get(key) == ((0, 2), (0.99, 0.95))


def test_cache_is_bounded_and_close_prevents_repopulation():
    cache = WindowCache(max_windows=2)
    for key in (b"a", b"b", b"c"):
        cache.put(key, ((0,), (1.0,)))
    assert cache.get(b"a") is None
    assert len(cache._entries) == 2
    cache.close()
    cache.put(b"later", ((0,), (1.0,)))
    assert not cache._entries


@pytest.mark.asyncio
async def test_reused_predictions_use_live_zero_width_offsets_and_attention_mask():
    detector = counting_detector()
    encoding = {
        "input_ids": [[65, 66], [65, 66], [65, 66]],
        "attention_mask": [[1, 1], [1, 1], [1, 0]],
        "offset_mapping": [[(0, 0), (1, 2)], [(5, 6), (6, 7)], [(8, 9), (9, 10)]],
    }

    @inference_request
    async def request():
        names = {"input_ids", "attention_mask"}
        a = detector._run_window(encoding, 0, names)
        b = detector._run_window(encoding, 1, names)
        detector._run_window(encoding, 2, names)
        assert a[2] == [(1, 2)]
        assert b[2] == [(5, 6), (6, 7)]
        assert len(a[0]) == 1 and len(b[0]) == 2

    await request()
    assert detector._session.calls == 2  # same IDs + mask reused; changed mask re-runs
