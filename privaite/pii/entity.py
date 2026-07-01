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

        # Overlap. The winner only decides the TYPE/score label; the merged span
        # covers the UNION of both, so a shorter (even higher-scored) detection can
        # never leave the uncovered remainder of a longer one unmasked. Entities
        # are sorted by (start asc, length desc), so last.start <= entity.start and
        # only the end can extend.
        if entity.entity_type == last.entity_type:
            winner = last if last.score >= entity.score else entity
        else:
            winner = _resolve_overlap(last, entity, overlap_resolution)

        new_start = last.start
        new_end = max(last.end, entity.end)
        if new_start == winner.start and new_end == winner.end:
            text = winner.text
        elif source_text is not None:
            text = source_text[new_start:new_end]
        else:
            # No source text (unit paths): fall back to the widest known text.
            text = last.text if last.length >= entity.length else entity.text

        merged[-1] = PIIEntity(
            entity_type=winner.entity_type,
            text=text,
            start=new_start,
            end=new_end,
            score=winner.score,
            source=winner.source,
        )

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
        a_presidio = a.source == "presidio"
        b_presidio = b.source == "presidio"
        if a_presidio != b_presidio:
            return a if a_presidio else b
        # Neither (or both) from presidio: fall back to score instead of blindly
        # keeping the later-sorted entity.
        return a if a.score >= b.score else b
    return a if a.score >= b.score else b
