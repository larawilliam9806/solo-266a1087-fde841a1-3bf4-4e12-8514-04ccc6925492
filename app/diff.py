"""Pure comparison of two frozen publication snapshots.

A publication freezes two kinds of records for the broadcast side:

``cue``     成片字幕: a reviewed line kept whole, one row per occurrence.
``review``  待复核片段: a reviewed line kept only partially.

Both kinds share the same identity triple -- 原片号 (source reel), 台词编号
(line number) and 成片出现次序 (occurrence order).  When the same line appears
several times in the finished film (a reused clip, or a line cut into several
pieces), the occurrences of *both* kinds are numbered together in finished-film
timecode order, which is what the subtitle editor checks one by one.

The module never touches the database: callers hand it two frozen publication
payloads and get back an immutable, deterministic diff.  Because the inputs are
snapshots, later subtitle or edit-table re-versions cannot change the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

CUE = "cue"
REVIEW = "review"

# Deterministic tie break when two records start at the same millisecond.
_KIND_RANK = {CUE: 0, REVIEW: 1}


@dataclass(frozen=True)
class FrozenRecord:
    """One cue or review item as stored in a publication snapshot."""

    kind: str
    source_reel_id: int
    source_reel_name: str | None
    line_number: int
    text: str
    start_ms: int
    end_ms: int

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.kind,
            "source_reel_id": self.source_reel_id,
            "source_reel_name": self.source_reel_name,
            "line_number": self.line_number,
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }
        if self.kind == REVIEW:
            payload["keep_start_ms"] = self.start_ms
            payload["keep_end_ms"] = self.end_ms
        return payload


def _records(publication: Mapping[str, Any]) -> list[FrozenRecord]:
    records: list[FrozenRecord] = []
    for cue in publication.get("subtitles", ()):
        records.append(
            FrozenRecord(
                kind=CUE,
                source_reel_id=cue["source_reel_id"],
                source_reel_name=cue.get("source_reel_name"),
                line_number=cue["line_number"],
                text=cue["text"],
                start_ms=cue["start_ms"],
                end_ms=cue["end_ms"],
            )
        )
    for review in publication.get("reviews", ()):
        records.append(
            FrozenRecord(
                kind=REVIEW,
                source_reel_id=review["source_reel_id"],
                source_reel_name=review.get("source_reel_name"),
                line_number=review["line_number"],
                text=review["text"],
                start_ms=review["keep_start_ms"],
                end_ms=review["keep_end_ms"],
            )
        )
    return records


def _index(publication: Mapping[str, Any]) -> dict[tuple[int, int, int], FrozenRecord]:
    """Index a snapshot by (reel, line number, finished-film occurrence).

    Occurrence counts cues and review items together, in finished-film timecode
    order: the nth appearance of a line is matched with the nth appearance in
    the other version, regardless of whether it was whole or partial.
    """

    ordered = sorted(
        _records(publication),
        key=lambda r: (
            r.start_ms,
            r.end_ms,
            r.source_reel_id,
            r.line_number,
            _KIND_RANK[r.kind],
        ),
    )
    seen: dict[tuple[int, int], int] = {}
    index: dict[tuple[int, int, int], FrozenRecord] = {}
    for record in ordered:
        pair = (record.source_reel_id, record.line_number)
        occurrence = seen.get(pair, 0)
        seen[pair] = occurrence + 1
        index[(*pair, occurrence)] = record
    return index


def _field_changes(base: FrozenRecord, target: FrozenRecord) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    if base.kind != target.kind:
        changes["type"] = {"base": base.kind, "target": target.kind}
    if base.text != target.text:
        changes["text"] = {"base": base.text, "target": target.text}
    if base.start_ms != target.start_ms or base.end_ms != target.end_ms:
        changes["time"] = {
            "base": {"start_ms": base.start_ms, "end_ms": base.end_ms},
            "target": {"start_ms": target.start_ms, "end_ms": target.end_ms},
        }
    return changes


def diff_publications(
    base: Mapping[str, Any], target: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare two successful publications of the same finished film.

    Returns added / deleted / changed / unchanged entries, keyed by source reel
    id, line number and finished-film occurrence.  The output order is fully
    deterministic so two calls on the same snapshots always agree.
    """

    base_index = _index(base)
    target_index = _index(target)

    added: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []

    for key in sorted(set(base_index) | set(target_index)):
        base_record = base_index.get(key)
        target_record = target_index.get(key)

        if base_record is None:
            assert target_record is not None
            added.append(
                {
                    "source_reel_id": key[0],
                    "line_number": key[1],
                    "occurrence": key[2],
                    "only_in": "target",
                    "record": target_record.to_payload(),
                }
            )
            continue
        if target_record is None:
            deleted.append(
                {
                    "source_reel_id": key[0],
                    "line_number": key[1],
                    "occurrence": key[2],
                    "only_in": "base",
                    "record": base_record.to_payload(),
                }
            )
            continue

        field_changes = _field_changes(base_record, target_record)
        entry = {
            "source_reel_id": key[0],
            "line_number": key[1],
            "occurrence": key[2],
            "base": base_record.to_payload(),
            "target": target_record.to_payload(),
            "changes": field_changes,
        }
        if field_changes:
            changed.append(entry)
        else:
            unchanged.append(entry)

    return {
        "film_id": base["film_id"],
        "film_name": base.get("film_name"),
        "base": _publication_ref(base),
        "target": _publication_ref(target),
        "summary": {
            "added": len(added),
            "deleted": len(deleted),
            "changed": len(changed),
            "unchanged": len(unchanged),
        },
        "added": added,
        "deleted": deleted,
        "changed": changed,
        "unchanged": unchanged,
    }


def _publication_ref(publication: Mapping[str, Any]) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "publication_id": publication["id"],
        "output_version": publication["output_version"],
        "edit_version": publication["edit_version"],
        "created_at": publication.get("created_at"),
    }
    if "delivery_id" in publication:
        ref["delivery_id"] = publication["delivery_id"]
    return ref
