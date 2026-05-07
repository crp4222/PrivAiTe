from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PIIEntity:
    entity_type: str
    text: str
    start: int
    end: int
    score: float
    source: str

    @property
    def length(self) -> int:
        return self.end - self.start


def merge_entities(
    entities: list[PIIEntity],
    strategy: str = "union",
    overlap_resolution: str = "highest_score",
    source_text: str | None = None,
) -> list[PIIEntity]:
    if not entities:
        return []

    sorted_entities = sorted(entities, key=lambda e: (e.start, -e.length))

    if strategy == "intersection":
        return _merge_intersection(sorted_entities, source_text)

    return _merge_union(sorted_entities, overlap_resolution, source_text)


def _merge_union(
    entities: list[PIIEntity],
    overlap_resolution: str,
    source_text: str | None = None,
) -> list[PIIEntity]:
    merged: list[PIIEntity] = []

    for entity in entities:
        if not merged:
            merged.append(entity)
            continue

        last = merged[-1]

        if entity.start >= last.end:
            merged.append(entity)
            continue

        if entity.entity_type == last.entity_type:
            winner = last if last.score >= entity.score else entity
            merged[-1] = winner
        else:
            winner = _resolve_overlap(last, entity, overlap_resolution)
            merged[-1] = winner

    return merged


def _merge_intersection(
    entities: list[PIIEntity], source_text: str | None = None
) -> list[PIIEntity]:
    if len(entities) < 2:
        return []

    result: list[PIIEntity] = []
    for i, e1 in enumerate(entities):
        for e2 in entities[i + 1 :]:
            if e1.start < e2.end and e2.start < e1.end:
                result.append(e1 if e1.score >= e2.score else e2)
                break

    return _merge_union(result, "highest_score", source_text) if result else []


def _resolve_overlap(
    a: PIIEntity, b: PIIEntity, resolution: str
) -> PIIEntity:
    if resolution == "longest_span":
        return a if a.length >= b.length else b
    if resolution == "presidio_priority":
        return a if a.source == "presidio" else b
    return a if a.score >= b.score else b
