"""`pii.max_concurrent_detections`: how many texts run through the detectors
at once. Each detection holds its detectors' working memory, so this is what
bounds peak memory when several requests arrive together."""

from __future__ import annotations

import asyncio
import threading
import time

from privaite.config.schema import (
    DetectionCacheConfig,
    DetectorsConfig,
    PIIConfig,
    PresidioDetectorConfig,
)
from privaite.pii.engine import PIIEngine
from privaite.pii.entity import PIIEntity


class SlowDetector:
    """Counts calls and the most that ran at once; each call takes a while, so
    requests sent together really overlap."""

    def __init__(self, seconds: float = 0.05) -> None:
        self.seconds = seconds
        self.calls = 0
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "slow"

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            await asyncio.to_thread(time.sleep, self.seconds)
            return (
                [PIIEntity("PERSON", "Marie", 0, 5, 0.9, "slow")]
                if text.startswith("Marie")
                else []
            )
        finally:
            with self._lock:
                self.running -= 1


def make_engine(limit: int | None, *, cache: bool = True, seconds: float = 0.05):
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
            detection_cache=DetectionCacheConfig(enabled=cache),
            max_concurrent_detections=limit,
        )
    )
    detector = SlowDetector(seconds)
    engine.detectors = [detector]
    return engine, detector


def test_unlimited_by_default():
    assert PIIConfig().max_concurrent_detections is None


async def test_distinct_texts_never_exceed_the_limit():
    engine, detector = make_engine(2, cache=False)

    await asyncio.gather(*(engine.inspect_text(f"text number {i}") for i in range(6)))

    assert detector.calls == 6
    assert detector.peak == 2


async def test_a_burst_of_the_same_new_text_costs_one_detection():
    engine, detector = make_engine(1)

    results = await asyncio.gather(*(engine.inspect_text("Marie wrote this") for _ in range(6)))

    assert detector.calls == 1
    assert all([(e.entity_type, e.text) for e in r] == [("PERSON", "Marie")] for r in results)


async def test_a_cached_text_does_not_wait_behind_a_detection():
    engine, detector = make_engine(1, seconds=0.5)
    await engine.inspect_text("Marie was here before")
    detector.seconds = 0.5

    slow = asyncio.create_task(engine.inspect_text("a new and slow text"))
    await asyncio.sleep(0.05)  # the slow detection now holds the only slot
    started = time.monotonic()
    cached = await engine.inspect_text("Marie was here before")
    waited = time.monotonic() - started
    await slow

    assert [(e.entity_type, e.text) for e in cached] == [("PERSON", "Marie")]
    assert waited < 0.25


async def test_without_a_limit_detections_overlap_as_before():
    engine, detector = make_engine(None, cache=False)

    await asyncio.gather(*(engine.inspect_text(f"text number {i}") for i in range(4)))

    assert detector.peak == 4
