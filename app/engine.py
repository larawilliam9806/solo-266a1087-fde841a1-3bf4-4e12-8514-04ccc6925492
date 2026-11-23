"""Pure computation for subtitle delivery.

The engine has no database or HTTP knowledge so the tricky rules can be tested
in isolation.

Intervals everywhere are ``[start, end)``: start inclusive, end exclusive.
Source clips are inserted at 1:1 speed, therefore the duration of a segment in
the finished film equals ``source_end_ms - source_start_ms``.

Two neighbouring edit segments from the same source reel are merged when the
source ranges touch *and* the output ranges touch.  A subtitle line is only
considered fully retained when a single merged block covers it end to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


class EditTableError(ValueError):
    """Raised when an edit table violates a structural rule."""

    def __init__(
        self, code: str, message: str, *, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class Segment:
    source_reel_id: int
    source_start_ms: int
    source_end_ms: int
    output_start_ms: int

    @property
    def duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms

    @property
    def output_end_ms(self) -> int:
        return self.output_start_ms + self.duration_ms


@dataclass(frozen=True)
class Line:
    number: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Block:
    """A maximal run of directly adjacent, mutually continuous segments."""

    source_reel_id: int
    source_start_ms: int
    source_end_ms: int
    output_start_ms: int

    @property
    def output_end_ms(self) -> int:
        return self.output_start_ms + (self.source_end_ms - self.source_start_ms)


@dataclass(frozen=True)
class Cue:
    source_reel_id: int
    line_number: int
    text: str
    start_ms: int
    end_ms: int
    source_start_ms: int
    source_end_ms: int


@dataclass(frozen=True)
class ReviewItem:
    source_reel_id: int
    line_number: int
    text: str
    keep_start_ms: int
    keep_end_ms: int


def normalize_segments(segments: Iterable[Segment]) -> list[Segment]:
    """Sort segments by output timecode and enforce non-overlap.

    Segments may be submitted in any order; the finished-film order is what
    makes two clips "adjacent".  Equal boundary values are fine because the
    end of an interval is exclusive.
    """

    ordered = sorted(
        segments,
        key=lambda s: (
            s.output_start_ms,
            s.source_reel_id,
            s.source_start_ms,
            s.source_end_ms,
        ),
    )

    if not ordered:
        return ordered

    for previous, current in zip(ordered, ordered[1:]):
        if current.output_start_ms < previous.output_end_ms:
            raise EditTableError(
                "segments_overlap",
                "edit segments overlap on the finished film",
                details={
                    "previous": {
                        "source_reel_id": previous.source_reel_id,
                        "output_start_ms": previous.output_start_ms,
                        "output_end_ms": previous.output_end_ms,
                    },
                    "current": {
                        "source_reel_id": current.source_reel_id,
                        "output_start_ms": current.output_start_ms,
                        "output_end_ms": current.output_end_ms,
                    },
                },
            )

    return ordered


def build_blocks(segments: Iterable[Segment]) -> list[Block]:
    """Merge consecutive segments continuous in both source and output."""

    ordered = normalize_segments(segments)
    blocks: list[Block] = []

    for segment in ordered:
        last = blocks[-1] if blocks else None
        if (
            last is not None
            and last.source_reel_id == segment.source_reel_id
            and last.source_end_ms == segment.source_start_ms
            and last.output_end_ms == segment.output_start_ms
        ):
            blocks[-1] = Block(
                source_reel_id=last.source_reel_id,
                source_start_ms=last.source_start_ms,
                source_end_ms=segment.source_end_ms,
                output_start_ms=last.output_start_ms,
            )
        else:
            blocks.append(
                Block(
                    source_reel_id=segment.source_reel_id,
                    source_start_ms=segment.source_start_ms,
                    source_end_ms=segment.source_end_ms,
                    output_start_ms=segment.output_start_ms,
                )
            )

    return blocks


def compute(
    segments: Sequence[Segment],
    subtitles: Mapping[int, Sequence[Line]],
) -> tuple[list[Cue], list[ReviewItem]]:
    """Produce finished-film cues and partial-line review items.

    ``subtitles`` maps a source reel id to its reviewed subtitle lines.  Every
    reel referenced by the edit table should be present; a missing key simply
    contributes no dialogue.
    """

    blocks = build_blocks(segments)
    blocks_by_reel: dict[int, list[Block]] = {}
    for block in blocks:
        blocks_by_reel.setdefault(block.source_reel_id, []).append(block)

    cues: list[Cue] = []
    reviews: list[ReviewItem] = []

    for reel_id, lines in subtitles.items():
        reel_blocks = blocks_by_reel.get(reel_id, [])
        if not reel_blocks:
            continue

        for line in lines:
            for block in reel_blocks:
                covered_start = max(line.start_ms, block.source_start_ms)
                covered_end = min(line.end_ms, block.source_end_ms)
                if covered_start >= covered_end:
                    continue

                output_start = block.output_start_ms + (
                    covered_start - block.source_start_ms
                )
                output_end = block.output_start_ms + (
                    covered_end - block.source_start_ms
                )

                if (
                    covered_start == line.start_ms
                    and covered_end == line.end_ms
                ):
                    cues.append(
                        Cue(
                            source_reel_id=reel_id,
                            line_number=line.number,
                            text=line.text,
                            start_ms=output_start,
                            end_ms=output_end,
                            source_start_ms=covered_start,
                            source_end_ms=covered_end,
                        )
                    )
                else:
                    reviews.append(
                        ReviewItem(
                            source_reel_id=reel_id,
                            line_number=line.number,
                            text=line.text,
                            keep_start_ms=output_start,
                            keep_end_ms=output_end,
                        )
                    )

    cues.sort(
        key=lambda c: (
            c.start_ms,
            c.end_ms,
            c.source_reel_id,
            c.line_number,
        )
    )
    reviews.sort(
        key=lambda r: (
            r.keep_start_ms,
            r.keep_end_ms,
            r.source_reel_id,
            r.line_number,
        )
    )
    return cues, reviews
