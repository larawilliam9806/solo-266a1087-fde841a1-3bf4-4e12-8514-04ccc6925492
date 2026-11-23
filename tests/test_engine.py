from __future__ import annotations

import pytest

from app import engine


def seg(reel, s, e, out):
    return engine.Segment(
        source_reel_id=reel, source_start_ms=s, source_end_ms=e, output_start_ms=out
    )


def line(n, s, e, text=None):
    return engine.Line(number=n, start_ms=s, end_ms=e, text=text or f"line {n}")


def test_fully_retained_line_uses_output_timecode():
    cues, reviews = engine.compute([seg(1, 10_000, 20_000, 5_000)], {1: [line(1, 12_000, 13_500)]})

    assert reviews == []
    assert len(cues) == 1
    assert (cues[0].start_ms, cues[0].end_ms) == (7_000, 8_500)


def test_line_exactly_covering_segment_is_retained():
    cues, reviews = engine.compute([seg(1, 0, 1_000, 4_000)], {1: [line(1, 0, 1_000)]})

    assert reviews == []
    assert [(c.start_ms, c.end_ms) for c in cues] == [(4_000, 5_000)]


def test_half_open_boundaries_do_not_count_as_overlap():
    segments = [seg(1, 0, 1_000, 0), seg(1, 1_000, 2_000, 1_000)]
    blocks = engine.build_blocks(segments)
    assert len(blocks) == 1
    assert (blocks[0].source_start_ms, blocks[0].source_end_ms) == (0, 2_000)


def test_adjacent_in_source_and_output_merge_and_keep_whole_line():
    segments = [seg(1, 0, 1_000, 0), seg(1, 1_000, 2_000, 1_000)]
    cues, reviews = engine.compute(segments, {1: [line(7, 500, 1_500)]})

    assert reviews == []
    assert [(c.start_ms, c.end_ms) for c in cues] == [(500, 1_500)]


def test_adjacent_segments_merge_even_when_submitted_out_of_order():
    segments = [seg(1, 1_000, 2_000, 1_000), seg(1, 0, 1_000, 0)]
    cues, reviews = engine.compute(segments, {1: [line(7, 500, 1_500)]})

    assert reviews == []
    assert [(c.start_ms, c.end_ms) for c in cues] == [(500, 1_500)]


def test_continuous_in_output_but_not_in_source_is_not_merged():
    # second clip jumps 5s ahead in the source film
    segments = [seg(1, 0, 1_000, 0), seg(1, 6_000, 7_000, 1_000)]
    cues, reviews = engine.compute(segments, {1: [line(3, 500, 6_500)]})

    assert cues == []
    kept = [(r.keep_start_ms, r.keep_end_ms) for r in reviews]
    assert kept == [(500, 1_000), (1_000, 1_500)]


def test_continuous_in_source_but_not_in_output_is_not_merged():
    segments = [seg(1, 0, 1_000, 0), seg(1, 1_000, 2_000, 5_000)]
    cues, reviews = engine.compute(segments, {1: [line(3, 500, 1_500)]})

    assert cues == []
    assert [(r.keep_start_ms, r.keep_end_ms) for r in reviews] == [(500, 1_000), (5_000, 5_500)]


def test_gap_dropping_the_middle_yields_two_review_pieces():
    segments = [seg(1, 0, 1_000, 0), seg(1, 2_000, 3_000, 1_000)]
    cues, reviews = engine.compute(segments, {1: [line(9, 0, 3_000)]})

    assert cues == []
    assert [(r.keep_start_ms, r.keep_end_ms) for r in reviews] == [(0, 1_000), (1_000, 2_000)]


def test_reused_clip_emits_one_cue_per_occurrence():
    segments = [seg(1, 0, 1_000, 0), seg(1, 0, 1_000, 10_000)]
    cues, reviews = engine.compute(segments, {1: [line(2, 100, 900)]})

    assert reviews == []
    assert [(c.start_ms, c.end_ms) for c in cues] == [(100, 900), (10_100, 10_900)]
    assert {c.line_number for c in cues} == {2}


def test_reused_clip_with_trimmed_line_reviews_each_place():
    segments = [seg(1, 0, 500, 0), seg(1, 0, 1_000, 2_000)]
    cues, reviews = engine.compute(segments, {1: [line(4, 0, 1_000)]})

    assert [(c.start_ms, c.end_ms) for c in cues] == [(2_000, 3_000)]
    assert [(r.keep_start_ms, r.keep_end_ms) for r in reviews] == [(0, 500)]


def test_line_survives_three_continuous_pieces():
    segments = [
        seg(2, 100, 200, 0),
        seg(2, 200, 300, 100),
        seg(2, 300, 400, 200),
    ]
    cues, reviews = engine.compute(segments, {2: [line(1, 50, 350)]})

    assert cues == []
    assert [(r.keep_start_ms, r.keep_end_ms) for r in reviews] == [(0, 250)]


def test_overlapping_output_segments_are_rejected():
    with pytest.raises(engine.EditTableError):
        engine.normalize_segments([seg(1, 0, 1_000, 0), seg(2, 0, 1_000, 500)])


def test_lines_from_unused_reels_are_ignored():
    cues, reviews = engine.compute([seg(1, 0, 1_000, 0)], {1: [], 2: [line(1, 0, 500)]})

    assert cues == [] and reviews == []


def test_line_outside_every_clip_produces_nothing():
    cues, reviews = engine.compute([seg(1, 0, 1_000, 0)], {1: [line(1, 5_000, 6_000)]})

    assert cues == [] and reviews == []


def test_line_touching_a_block_boundary_is_not_covered():
    # line ends exactly where the clip starts / starts exactly where it ends
    cues, reviews = engine.compute(
        [seg(1, 1_000, 2_000, 0)], {1: [line(1, 0, 1_000), line(2, 2_000, 3_000)]}
    )

    assert cues == [] and reviews == []


def test_clips_from_different_reels_never_merge():
    segments = [seg(1, 0, 1_000, 0), seg(2, 0, 1_000, 1_000)]
    blocks = engine.build_blocks(segments)
    assert len(blocks) == 2

    cues, reviews = engine.compute(
        segments, {1: [line(1, 0, 1_000)], 2: [line(1, 0, 1_000)]}
    )
    assert [(c.source_reel_id, c.start_ms, c.end_ms) for c in cues] == [
        (1, 0, 1_000),
        (2, 1_000, 2_000),
    ]
    assert reviews == []


def test_segment_order_does_not_affect_review_order():
    segments = [seg(1, 2_000, 3_000, 1_000), seg(1, 0, 1_000, 0)]
    cues, reviews = engine.compute(segments, {1: [line(1, 0, 3_000)]})

    assert cues == []
    assert [(r.keep_start_ms, r.keep_end_ms) for r in reviews] == [(0, 1_000), (1_000, 2_000)]


def test_reused_clip_mixed_full_and_partial_occurrences():
    segments = [seg(1, 0, 1_000, 0), seg(1, 2_000, 3_000, 1_000), seg(1, 0, 1_000, 5_000)]
    cues, reviews = engine.compute(
        segments, {1: [line(1, 0, 1_000), line(2, 500, 1_500)]}
    )

    assert [(c.line_number, c.start_ms, c.end_ms) for c in cues] == [
        (1, 0, 1_000),
        (1, 5_000, 6_000),
    ]
    assert [(r.line_number, r.keep_start_ms, r.keep_end_ms) for r in reviews] == [
        (2, 500, 1_000),
        (2, 5_500, 6_000),
    ]
