from __future__ import annotations

from app.diff import diff_publications


def _pub(output_version, *, cues=(), reviews=(), film_id=1, publication_id=None):
    return {
        "id": publication_id if publication_id is not None else output_version,
        "film_id": film_id,
        "film_name": "成片",
        "delivery_id": output_version * 10,
        "output_version": output_version,
        "edit_version": output_version,
        "created_at": f"2026-01-0{output_version}T00:00:00Z",
        "subtitles": list(cues),
        "reviews": list(reviews),
    }


def cue(reel, number, start, end, text=None):
    return {
        "source_reel_id": reel,
        "source_reel_name": f"r{reel}",
        "line_number": number,
        "text": text or f"句{number}",
        "start_ms": start,
        "end_ms": end,
    }


def review(reel, number, start, end, text=None):
    return {
        "source_reel_id": reel,
        "source_reel_name": f"r{reel}",
        "line_number": number,
        "text": text or f"句{number}",
        "keep_start_ms": start,
        "keep_end_ms": end,
    }


def test_identical_publications_report_everything_unchanged():
    pub = _pub(1, cues=[cue(1, 1, 0, 1000), cue(1, 2, 2000, 3000)])
    result = diff_publications(pub, pub)

    assert result["summary"] == {
        "added": 0,
        "deleted": 0,
        "changed": 0,
        "unchanged": 2,
    }
    assert result["added"] == []
    assert result["deleted"] == []
    assert result["changed"] == []
    assert [(e["source_reel_id"], e["line_number"], e["occurrence"]) for e in result["unchanged"]] == [
        (1, 1, 0),
        (1, 2, 0),
    ]
    assert result["unchanged"][0]["changes"] == {}


def test_new_record_is_added_and_missing_record_is_deleted():
    base = _pub(1, cues=[cue(1, 1, 0, 1000)])
    target = _pub(
        2,
        cues=[cue(1, 1, 0, 1000), cue(1, 2, 2000, 3000, "新句")],
    )
    result = diff_publications(base, target)

    assert result["summary"] == {"added": 1, "deleted": 0, "changed": 0, "unchanged": 1}
    added = result["added"][0]
    assert added["only_in"] == "target"
    assert added["line_number"] == 2
    assert added["record"]["text"] == "新句"
    assert added["record"]["type"] == "cue"

    reverse = diff_publications(target, base)
    assert reverse["summary"]["deleted"] == 1
    assert reverse["deleted"][0]["only_in"] == "base"
    assert reverse["deleted"][0]["line_number"] == 2


def test_review_kept_ranges_are_compared_as_time_changes():
    base = _pub(1, reviews=[review(1, 5, 1000, 2000)])
    target = _pub(2, reviews=[review(1, 5, 1500, 2000)])
    result = diff_publications(base, target)

    assert result["summary"]["changed"] == 1
    entry = result["changed"][0]
    assert entry["changes"] == {
        "time": {
            "base": {"start_ms": 1000, "end_ms": 2000},
            "target": {"start_ms": 1500, "end_ms": 2000},
        }
    }
    assert entry["base"]["keep_start_ms"] == 1000
    assert entry["target"]["keep_start_ms"] == 1500


def test_text_change_is_reported_separately_from_time_change():
    base = _pub(1, cues=[cue(1, 1, 0, 1000, "旧文字")])
    target = _pub(2, cues=[cue(1, 1, 0, 1000, "新文字")])
    result = diff_publications(base, target)

    assert list(result["changed"][0]["changes"]) == ["text"]
    assert result["changed"][0]["changes"]["text"] == {
        "base": "旧文字",
        "target": "新文字",
    }


def test_type_change_between_cue_and_review_is_flagged():
    base = _pub(1, cues=[cue(1, 3, 0, 1000)])
    target = _pub(2, reviews=[review(1, 3, 0, 800)])
    result = diff_publications(base, target)

    changes = result["changed"][0]["changes"]
    assert changes["type"] == {"base": "cue", "target": "review"}
    assert "time" in changes


def test_records_are_matched_by_occurrence_in_finished_film_order():
    # Same line reused twice; the second occurrence shifts, the first stays.
    base = _pub(
        1,
        cues=[
            cue(1, 9, 0, 1000),
            cue(1, 9, 5000, 6000),
        ],
    )
    target = _pub(
        2,
        cues=[
            cue(1, 9, 0, 1000),
            cue(1, 9, 7000, 8000),
        ],
    )
    result = diff_publications(base, target)

    assert result["summary"] == {"added": 0, "deleted": 0, "changed": 1, "unchanged": 1}
    changed = result["changed"][0]
    assert changed["occurrence"] == 1
    assert changed["changes"]["time"]["target"]["start_ms"] == 7000
    assert result["unchanged"][0]["occurrence"] == 0


def test_cue_and_review_occurrences_share_one_sequence_per_line():
    # Occurrence 0 is a cue in both versions; occurrence 1 changes review range.
    base = _pub(
        1,
        cues=[cue(1, 4, 0, 1000)],
        reviews=[review(1, 4, 5000, 5500)],
    )
    target = _pub(
        2,
        cues=[cue(1, 4, 0, 1000)],
        reviews=[review(1, 4, 5000, 5800)],
    )
    result = diff_publications(base, target)

    assert result["summary"]["changed"] == 1
    entry = result["changed"][0]
    assert entry["occurrence"] == 1
    assert entry["base"]["type"] == "review"


def test_records_only_in_one_version_are_marked_with_side():
    base = _pub(1, cues=[cue(1, 1, 0, 1000)], reviews=[review(2, 7, 0, 500)])
    target = _pub(2, cues=[cue(1, 1, 0, 1000), cue(3, 8, 900, 950)])
    result = diff_publications(base, target)

    deleted = result["deleted"][0]
    assert deleted["only_in"] == "base"
    assert deleted["record"]["type"] == "review"
    assert deleted["record"]["keep_end_ms"] == 500

    added = result["added"][0]
    assert added["only_in"] == "target"
    assert added["source_reel_id"] == 3


def test_matching_uses_source_reel_and_line_number_not_position():
    base = _pub(
        1,
        cues=[cue(1, 1, 0, 1000), cue(2, 1, 4000, 5000, "二号机")],
    )
    target = _pub(
        2,
        cues=[
            cue(2, 1, 4000, 5000, "二号机"),
            cue(1, 1, 200, 1200),
        ],
    )
    result = diff_publications(base, target)

    # Two distinct (reel, line) identities: reel 1 moved in time, reel 2 unchanged.
    changed = result["changed"][0]
    assert changed["source_reel_id"] == 1
    assert changed["changes"]["time"]["base"]["start_ms"] == 0
    assert result["unchanged"][0]["source_reel_id"] == 2


def test_result_order_is_deterministic_and_summary_covers_all_records():
    base = _pub(
        1,
        cues=[cue(1, 1, 0, 1000), cue(1, 2, 2000, 3000), cue(1, 3, 4000, 5000)],
    )
    target = _pub(
        2,
        cues=[cue(1, 2, 2100, 3000), cue(1, 3, 4000, 5000), cue(1, 4, 6000, 7000)],
    )
    first = diff_publications(base, target)
    second = diff_publications(base, target)

    assert first == second
    total = sum(first["summary"].values())
    assert total == 4
    assert [e["line_number"] for e in first["deleted"]] == [1]
    assert [e["line_number"] for e in first["added"]] == [4]
    assert [e["line_number"] for e in first["changed"]] == [2]
    assert [e["line_number"] for e in first["unchanged"]] == [3]


def test_refs_identify_both_publication_snapshots():
    base = _pub(1, cues=[cue(1, 1, 0, 1000)])
    target = _pub(3, cues=[cue(1, 1, 0, 1000)])
    result = diff_publications(base, target)

    assert result["base"]["output_version"] == 1
    assert result["base"]["publication_id"] == 1
    assert result["target"]["output_version"] == 3
    assert result["target"]["edit_version"] == 3
    assert result["film_id"] == 1
