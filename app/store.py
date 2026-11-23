"""Domain logic on top of SQLite.

Every mutating operation takes an exclusive transaction, so version numbers are
assigned exactly once and a publication is either fully recorded or not at all.

Terminology used throughout:

``source reel``   原片
``film``          成片
``subtitle doc``  已审台词的某个版本
``edit table``    剪辑表的某个版本
``delivery``      一次交付：锁定某个剪辑表版本 + 其引用原片的字幕版本
``publication``   成功交付后冻结下来的成片字幕与待复核片段
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Sequence

from . import engine
from .engine import EditTableError, Line, Segment
from .errors import Conflict, Invalid, NotFound

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _one(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _next_version(conn: sqlite3.Connection, table: str, column: str, value: int) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(version), 0) + 1 AS next FROM {table} WHERE {column} = ?",
        (value,),
    ).fetchone()
    return int(row["next"])


def _segments_from_rows(rows: Iterable[dict]) -> list[Segment]:
    return [
        Segment(
            source_reel_id=row["source_reel_id"],
            source_start_ms=row["source_start_ms"],
            source_end_ms=row["source_end_ms"],
            output_start_ms=row["output_start_ms"],
        )
        for row in rows
    ]


def _lines_from_rows(rows: Iterable[dict]) -> list[Line]:
    return [
        Line(
            number=row["number"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            text=row["text"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# source reels and subtitles
# ---------------------------------------------------------------------------


def create_source_reel(conn: sqlite3.Connection, name: str) -> dict:
    name = name.strip()
    if not name:
        raise Invalid("invalid_name", "source reel name must not be blank")
    if conn.execute("SELECT 1 FROM source_reels WHERE name = ?", (name,)).fetchone():
        raise Conflict(
            "source_reel_exists", f"source reel named {name!r} already exists"
        )
    cur = conn.execute("INSERT INTO source_reels (name) VALUES (?)", (name,))
    return get_source_reel(conn, int(cur.lastrowid))


def list_source_reels(conn: sqlite3.Connection) -> list[dict]:
    return _all(
        conn,
        """
        SELECT r.id, r.name, r.created_at,
               (SELECT MAX(d.version) FROM subtitle_documents d
                 WHERE d.source_reel_id = r.id) AS latest_subtitle_version
          FROM source_reels r
         ORDER BY r.id
        """,
    )


def get_source_reel(conn: sqlite3.Connection, reel_id: int) -> dict:
    row = _one(
        conn,
        """
        SELECT r.id, r.name, r.created_at,
               (SELECT MAX(d.version) FROM subtitle_documents d
                 WHERE d.source_reel_id = r.id) AS latest_subtitle_version
          FROM source_reels r WHERE r.id = ?
        """,
        (reel_id,),
    )
    if row is None:
        raise NotFound("source_reel_not_found", f"source reel {reel_id} does not exist")
    return row


def _document_payload(conn: sqlite3.Connection, document: dict) -> dict:
    lines = _all(
        conn,
        """
        SELECT number, start_ms, end_ms, text
          FROM subtitle_lines
         WHERE document_id = ?
         ORDER BY start_ms, end_ms, number
        """,
        (document["id"],),
    )
    return {
        "source_reel_id": document["source_reel_id"],
        "version": document["version"],
        "created_at": document["created_at"],
        "lines": lines,
    }


def create_subtitle_version(
    conn: sqlite3.Connection, reel_id: int, lines: Sequence[dict]
) -> dict:
    """Store a new subtitle version, even if its content is unchanged.

    A re-submission is itself a signal: any delivery that pinned the previous
    version must be returned, because the subtitle editor has re-opened it.
    """

    get_source_reel(conn, reel_id)
    version = _next_version(conn, "subtitle_documents", "source_reel_id", reel_id)
    cur = conn.execute(
        "INSERT INTO subtitle_documents (source_reel_id, version) VALUES (?, ?)",
        (reel_id, version),
    )
    document_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO subtitle_lines (document_id, number, start_ms, end_ms, text)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (document_id, line["number"], line["start_ms"], line["end_ms"], line["text"])
            for line in lines
        ],
    )
    document = _one(
        conn,
        "SELECT id, source_reel_id, version, created_at FROM subtitle_documents WHERE id = ?",
        (document_id,),
    )
    assert document is not None
    return _document_payload(conn, document)


def latest_subtitle_version(conn: sqlite3.Connection, reel_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM subtitle_documents WHERE source_reel_id = ?",
        (reel_id,),
    ).fetchone()
    return int(row["v"])


def get_subtitle_document(
    conn: sqlite3.Connection, reel_id: int, version: int | None = None
) -> dict | None:
    get_source_reel(conn, reel_id)
    if version is None:
        version = latest_subtitle_version(conn, reel_id)
        if version == 0:
            return None
    document = _one(
        conn,
        """
        SELECT id, source_reel_id, version, created_at FROM subtitle_documents
         WHERE source_reel_id = ? AND version = ?
        """,
        (reel_id, version),
    )
    if document is None:
        raise NotFound(
            "subtitle_version_not_found",
            f"source reel {reel_id} has no subtitle version {version}",
        )
    return _document_payload(conn, document)


def list_subtitle_versions(conn: sqlite3.Connection, reel_id: int) -> list[dict]:
    get_source_reel(conn, reel_id)
    return _all(
        conn,
        """
        SELECT version, created_at,
               (SELECT COUNT(*) FROM subtitle_lines l WHERE l.document_id = d.id) AS line_count
          FROM subtitle_documents d
         WHERE source_reel_id = ?
         ORDER BY version
        """,
        (reel_id,),
    )


# ---------------------------------------------------------------------------
# films and edit tables
# ---------------------------------------------------------------------------


def create_film(conn: sqlite3.Connection, name: str) -> dict:
    name = name.strip()
    if not name:
        raise Invalid("invalid_name", "film name must not be blank")
    if conn.execute("SELECT 1 FROM films WHERE name = ?", (name,)).fetchone():
        raise Conflict("film_exists", f"film named {name!r} already exists")
    cur = conn.execute("INSERT INTO films (name) VALUES (?)", (name,))
    return get_film(conn, int(cur.lastrowid))


def list_films(conn: sqlite3.Connection) -> list[dict]:
    return _all(
        conn,
        """
        SELECT f.id, f.name, f.created_at,
               (SELECT MAX(t.version) FROM edit_tables t WHERE t.film_id = f.id)
                   AS latest_edit_version,
               (SELECT MAX(p.output_version) FROM publications p WHERE p.film_id = f.id)
                   AS latest_output_version
          FROM films f
         ORDER BY f.id
        """,
    )


def get_film(conn: sqlite3.Connection, film_id: int) -> dict:
    row = _one(
        conn,
        """
        SELECT f.id, f.name, f.created_at,
               (SELECT MAX(t.version) FROM edit_tables t WHERE t.film_id = f.id)
                   AS latest_edit_version,
               (SELECT MAX(p.output_version) FROM publications p WHERE p.film_id = f.id)
                   AS latest_output_version
          FROM films f WHERE f.id = ?
        """,
        (film_id,),
    )
    if row is None:
        raise NotFound("film_not_found", f"film {film_id} does not exist")
    return row


def _edit_table_payload(conn: sqlite3.Connection, table: dict) -> dict:
    segments = _all(
        conn,
        """
        SELECT s.position, s.source_reel_id, r.name AS source_reel_name,
               s.source_start_ms, s.source_end_ms, s.output_start_ms,
               (s.source_end_ms - s.source_start_ms) AS duration_ms
          FROM edit_segments s
          JOIN source_reels r ON r.id = s.source_reel_id
         WHERE s.edit_table_id = ?
         ORDER BY s.position
        """,
        (table["id"],),
    )
    return {
        "film_id": table["film_id"],
        "version": table["version"],
        "created_at": table["created_at"],
        "segments": segments,
    }


def create_edit_table(
    conn: sqlite3.Connection, film_id: int, segments: Sequence[dict]
) -> dict:
    get_film(conn, film_id)

    missing = sorted(
        {
            segment["source_reel_id"]
            for segment in segments
            if conn.execute(
                "SELECT 1 FROM source_reels WHERE id = ?",
                (segment["source_reel_id"],),
            ).fetchone()
            is None
        }
    )
    if missing:
        raise Invalid(
            "unknown_source_reel",
            "edit table references unknown source reels",
            details={"source_reel_ids": missing},
        )

    try:
        ordered = engine.normalize_segments(
            [
                Segment(
                    source_reel_id=segment["source_reel_id"],
                    source_start_ms=segment["source_start_ms"],
                    source_end_ms=segment["source_end_ms"],
                    output_start_ms=segment["output_start_ms"],
                )
                for segment in segments
            ]
        )
    except EditTableError as exc:
        raise Invalid(exc.code, exc.message, details=exc.details) from exc

    version = _next_version(conn, "edit_tables", "film_id", film_id)
    cur = conn.execute(
        "INSERT INTO edit_tables (film_id, version) VALUES (?, ?)",
        (film_id, version),
    )
    table_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO edit_segments
            (edit_table_id, position, source_reel_id,
             source_start_ms, source_end_ms, output_start_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                table_id,
                position,
                segment.source_reel_id,
                segment.source_start_ms,
                segment.source_end_ms,
                segment.output_start_ms,
            )
            for position, segment in enumerate(ordered)
        ],
    )
    table = _one(
        conn,
        "SELECT id, film_id, version, created_at FROM edit_tables WHERE id = ?",
        (table_id,),
    )
    assert table is not None
    return _edit_table_payload(conn, table)


def get_edit_table(
    conn: sqlite3.Connection, film_id: int, version: int | None = None
) -> dict | None:
    get_film(conn, film_id)
    if version is None:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM edit_tables WHERE film_id = ?", (film_id,)
        ).fetchone()
        if row is None or row["v"] is None:
            return None
        version = int(row["v"])
    table = _one(
        conn,
        """
        SELECT id, film_id, version, created_at FROM edit_tables
         WHERE film_id = ? AND version = ?
        """,
        (film_id, version),
    )
    if table is None:
        raise NotFound(
            "edit_version_not_found",
            f"film {film_id} has no edit table version {version}",
        )
    return _edit_table_payload(conn, table)


def list_edit_versions(conn: sqlite3.Connection, film_id: int) -> list[dict]:
    get_film(conn, film_id)
    return _all(
        conn,
        """
        SELECT version, created_at,
               (SELECT COUNT(*) FROM edit_segments s WHERE s.edit_table_id = t.id)
                   AS segment_count
          FROM edit_tables t
         WHERE film_id = ?
         ORDER BY version
        """,
        (film_id,),
    )


# ---------------------------------------------------------------------------
# deliveries
# ---------------------------------------------------------------------------


def _pins(conn: sqlite3.Connection, delivery_id: int) -> dict[int, int]:
    rows = _all(
        conn,
        "SELECT source_reel_id, subtitle_version FROM delivery_pins WHERE delivery_id = ?",
        (delivery_id,),
    )
    return {row["source_reel_id"]: row["subtitle_version"] for row in rows}


def _pin_payload(conn: sqlite3.Connection, pins: dict[int, int]) -> list[dict]:
    out = []
    for reel_id in sorted(pins):
        reel = get_source_reel(conn, reel_id)
        out.append(
            {
                "source_reel_id": reel_id,
                "source_reel_name": reel["name"],
                "subtitle_version": pins[reel_id],
            }
        )
    return out


def create_delivery(conn: sqlite3.Connection, film_id: int) -> dict:
    """Freeze the film's current edit table and referenced subtitle versions.

    A delivery is a snapshot of intent: the exact edit table version and the
    exact subtitle version of every source reel that edit table references.
    From here on the snapshot never changes, so if any of those versions is
    replaced before publishing, the delivery must be returned and a new one
    submitted.
    """

    get_film(conn, film_id)

    row = conn.execute(
        "SELECT MAX(version) AS v FROM edit_tables WHERE film_id = ?", (film_id,)
    ).fetchone()
    if row is None or row["v"] is None:
        raise Conflict(
            "no_edit_table",
            f"film {film_id} has no edit table to deliver",
        )
    edit_version = int(row["v"])

    table = _one(
        conn,
        "SELECT id, version FROM edit_tables WHERE film_id = ? AND version = ?",
        (film_id, edit_version),
    )
    assert table is not None

    referenced = [
        row["source_reel_id"]
        for row in conn.execute(
            "SELECT DISTINCT source_reel_id FROM edit_segments WHERE edit_table_id = ? ORDER BY source_reel_id",
            (table["id"],),
        ).fetchall()
    ]

    pins = {
        reel_id: latest_subtitle_version(conn, reel_id) for reel_id in referenced
    }

    cur = conn.execute(
        "INSERT INTO deliveries (film_id, edit_table_id) VALUES (?, ?)",
        (film_id, table["id"]),
    )
    delivery_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO delivery_pins (delivery_id, source_reel_id, subtitle_version) VALUES (?, ?, ?)",
        [(delivery_id, reel_id, pins[reel_id]) for reel_id in sorted(pins)],
    )
    return delivery_detail(conn, delivery_id)


def _delivery_row(conn: sqlite3.Connection, delivery_id: int) -> dict:
    row = _one(
        conn,
        """
        SELECT d.id, d.film_id, f.name AS film_name, d.state, d.edit_table_id,
               d.reject_reason, d.created_at, d.published_at,
               t.version AS edit_table_version
          FROM deliveries d
          JOIN films f ON f.id = d.film_id
          JOIN edit_tables t ON t.id = d.edit_table_id
         WHERE d.id = ?
        """,
        (delivery_id,),
    )
    if row is None:
        raise NotFound("delivery_not_found", f"delivery {delivery_id} does not exist")
    return row


def _staleness(
    conn: sqlite3.Connection, row: dict, pins: dict[int, int]
) -> dict:
    """Compare the delivery's pins with the current versions.

    Only the film's current edit table and the source reels referenced by the
    *pinned* edit table are examined.
    """

    changes: dict = {}

    current_edit = conn.execute(
        "SELECT MAX(version) AS v FROM edit_tables WHERE film_id = ?",
        (row["film_id"],),
    ).fetchone()
    current_edit_version = int(current_edit["v"]) if current_edit["v"] is not None else 0
    if current_edit_version != row["edit_table_version"]:
        changes["edit_table"] = {
            "pinned": row["edit_table_version"],
            "current": current_edit_version,
        }

    sources = []
    for reel_id in sorted(pins):
        current = latest_subtitle_version(conn, reel_id)
        if current != pins[reel_id]:
            sources.append(
                {
                    "source_reel_id": reel_id,
                    "pinned": pins[reel_id],
                    "current": current,
                }
            )
    if sources:
        changes["sources"] = sources

    return changes


def _compute(
    conn: sqlite3.Connection, edit_table_id: int, pins: dict[int, int]
) -> tuple[list[dict], list[dict]]:
    segment_rows = _all(
        conn,
        """
        SELECT source_reel_id, source_start_ms, source_end_ms, output_start_ms
          FROM edit_segments WHERE edit_table_id = ? ORDER BY position
        """,
        (edit_table_id,),
    )
    segments = _segments_from_rows(segment_rows)

    subtitles: dict[int, list[Line]] = {}
    for reel_id, version in pins.items():
        if version == 0:
            continue
        doc = conn.execute(
            """
            SELECT id FROM subtitle_documents
             WHERE source_reel_id = ? AND version = ?
            """,
            (reel_id, version),
        ).fetchone()
        if doc is None:
            continue
        line_rows = _all(
            conn,
            """
            SELECT number, start_ms, end_ms, text FROM subtitle_lines
             WHERE document_id = ? ORDER BY start_ms, end_ms, number
            """,
            (doc["id"],),
        )
        subtitles[reel_id] = _lines_from_rows(line_rows)

    cues, reviews = engine.compute(segments, subtitles)

    names = {
        row["id"]: row["name"]
        for row in conn.execute("SELECT id, name FROM source_reels").fetchall()
    }

    cue_payload = [
        {
            "source_reel_id": cue.source_reel_id,
            "source_reel_name": names.get(cue.source_reel_id),
            "line_number": cue.line_number,
            "text": cue.text,
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
            "source_start_ms": cue.source_start_ms,
            "source_end_ms": cue.source_end_ms,
        }
        for cue in cues
    ]
    review_payload = [
        {
            "source_reel_id": review.source_reel_id,
            "source_reel_name": names.get(review.source_reel_id),
            "line_number": review.line_number,
            "text": review.text,
            "keep_start_ms": review.keep_start_ms,
            "keep_end_ms": review.keep_end_ms,
        }
        for review in reviews
    ]
    return cue_payload, review_payload


def delivery_detail(conn: sqlite3.Connection, delivery_id: int) -> dict:
    row = _delivery_row(conn, delivery_id)
    pins = _pins(conn, delivery_id)
    # A closed delivery is history: only an open one can still drift.
    changes = _staleness(conn, row, pins) if row["state"] == "open" else {}

    publication = _one(
        conn,
        "SELECT id, output_version FROM publications WHERE delivery_id = ?",
        (delivery_id,),
    )

    payload = {
        "id": row["id"],
        "film_id": row["film_id"],
        "film_name": row["film_name"],
        "state": row["state"],
        "edit_table_version": row["edit_table_version"],
        "sources": _pin_payload(conn, pins),
        "created_at": row["created_at"],
        "published_at": row["published_at"],
        "stale": bool(changes),
        "changes": changes,
        "publication_id": publication["id"] if publication else None,
        "output_version": publication["output_version"] if publication else None,
    }
    if row["reject_reason"]:
        try:
            payload["rejection"] = json.loads(row["reject_reason"])
        except json.JSONDecodeError:  # pragma: no cover - defensive
            payload["rejection"] = {"message": row["reject_reason"]}
    return payload


def list_deliveries(conn: sqlite3.Connection, film_id: int) -> list[dict]:
    get_film(conn, film_id)
    rows = _all(
        conn,
        "SELECT id FROM deliveries WHERE film_id = ? ORDER BY id",
        (film_id,),
    )
    return [delivery_detail(conn, row["id"]) for row in rows]


def delivery_preview(conn: sqlite3.Connection, delivery_id: int) -> dict:
    row = _delivery_row(conn, delivery_id)
    pins = _pins(conn, delivery_id)
    changes = _staleness(conn, row, pins)
    cues, reviews = _compute(conn, row["edit_table_id"], pins)
    return {
        "delivery_id": row["id"],
        "film_id": row["film_id"],
        "state": row["state"],
        "edit_table_version": row["edit_table_version"],
        "sources": _pin_payload(conn, pins),
        "stale": bool(changes),
        "changes": changes,
        "subtitles": cues,
        "reviews": reviews,
    }


def publish_delivery(conn: sqlite3.Connection, delivery_id: int) -> tuple[bool, dict]:
    """Publish or reject.  Returns ``(published, payload)``.

    The caller is responsible for turning ``published == False`` into an HTTP
    error after the transaction commits (the rejection must be recorded).
    """

    row = _delivery_row(conn, delivery_id)
    if row["state"] != "open":
        raise Conflict(
            "delivery_not_open",
            f"delivery {delivery_id} is {row['state']}, only open deliveries can be published",
            details={"state": row["state"]},
        )

    pins = _pins(conn, delivery_id)
    changes = _staleness(conn, row, pins)

    if changes:
        rejection = {
            "code": "delivery_stale",
            "message": "edit table or referenced subtitles changed after the delivery was created",
            "delivery_id": delivery_id,
            "changes": changes,
        }
        conn.execute(
            "UPDATE deliveries SET state = 'rejected', reject_reason = ? WHERE id = ?",
            (json.dumps(rejection, ensure_ascii=False), delivery_id),
        )
        return False, rejection

    cues, reviews = _compute(conn, row["edit_table_id"], pins)

    next_output = conn.execute(
        "SELECT COALESCE(MAX(output_version), 0) + 1 AS v FROM publications WHERE film_id = ?",
        (row["film_id"],),
    ).fetchone()["v"]

    cur = conn.execute(
        """
        INSERT INTO publications
            (film_id, delivery_id, output_version, edit_version)
        VALUES (?, ?, ?, ?)
        """,
        (row["film_id"], delivery_id, next_output, row["edit_table_version"]),
    )
    publication_id = int(cur.lastrowid)

    names = {
        r["id"]: r["name"]
        for r in conn.execute("SELECT id, name FROM source_reels").fetchall()
    }
    conn.executemany(
        """
        INSERT INTO publication_sources
            (publication_id, source_reel_id, source_reel_name, subtitle_version)
        VALUES (?, ?, ?, ?)
        """,
        [
            (publication_id, reel_id, names[reel_id], pins[reel_id])
            for reel_id in sorted(pins)
        ],
    )
    conn.executemany(
        """
        INSERT INTO publication_cues
            (publication_id, ordinal, source_reel_id, source_reel_name,
             line_number, text, start_ms, end_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                publication_id,
                ordinal,
                cue["source_reel_id"],
                cue["source_reel_name"],
                cue["line_number"],
                cue["text"],
                cue["start_ms"],
                cue["end_ms"],
            )
            for ordinal, cue in enumerate(cues)
        ],
    )
    conn.executemany(
        """
        INSERT INTO publication_reviews
            (publication_id, ordinal, source_reel_id, source_reel_name,
             line_number, text, keep_start_ms, keep_end_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                publication_id,
                ordinal,
                review["source_reel_id"],
                review["source_reel_name"],
                review["line_number"],
                review["text"],
                review["keep_start_ms"],
                review["keep_end_ms"],
            )
            for ordinal, review in enumerate(reviews)
        ],
    )
    conn.execute(
        """
        UPDATE deliveries
           SET state = 'published',
               published_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
         WHERE id = ?
        """,
        (delivery_id,),
    )
    return True, get_publication(conn, publication_id)


# ---------------------------------------------------------------------------
# publications
# ---------------------------------------------------------------------------


def get_publication(conn: sqlite3.Connection, publication_id: int) -> dict:
    row = _one(
        conn,
        """
        SELECT p.id, p.film_id, f.name AS film_name, p.delivery_id,
               p.output_version, p.edit_version, p.created_at
          FROM publications p
          JOIN films f ON f.id = p.film_id
         WHERE p.id = ?
        """,
        (publication_id,),
    )
    if row is None:
        raise NotFound("publication_not_found", f"publication {publication_id} does not exist")

    sources = _all(
        conn,
        """
        SELECT source_reel_id, source_reel_name, subtitle_version
          FROM publication_sources
         WHERE publication_id = ?
         ORDER BY source_reel_id
        """,
        (publication_id,),
    )
    subtitles = _all(
        conn,
        """
        SELECT source_reel_id, source_reel_name, line_number, text,
               start_ms, end_ms
          FROM publication_cues
         WHERE publication_id = ?
         ORDER BY ordinal
        """,
        (publication_id,),
    )
    reviews = _all(
        conn,
        """
        SELECT source_reel_id, source_reel_name, line_number, text,
               keep_start_ms, keep_end_ms
          FROM publication_reviews
         WHERE publication_id = ?
         ORDER BY ordinal
        """,
        (publication_id,),
    )
    return {
        **row,
        "sources": sources,
        "subtitles": subtitles,
        "reviews": reviews,
    }


def latest_publication(
    conn: sqlite3.Connection, film_id: int, output_version: int | None = None
) -> dict:
    get_film(conn, film_id)
    if output_version is None:
        row = conn.execute(
            "SELECT id FROM publications WHERE film_id = ? ORDER BY output_version DESC LIMIT 1",
            (film_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM publications WHERE film_id = ? AND output_version = ?",
            (film_id, output_version),
        ).fetchone()
    if row is None:
        raise NotFound(
            "no_successful_delivery",
            f"film {film_id} has no successful delivery"
            + ("" if output_version is None else f" with output version {output_version}"),
        )
    return get_publication(conn, row["id"])


def list_publications(conn: sqlite3.Connection, film_id: int) -> list[dict]:
    get_film(conn, film_id)
    rows = _all(
        conn,
        "SELECT id FROM publications WHERE film_id = ? ORDER BY output_version",
        (film_id,),
    )
    return [get_publication(conn, row["id"]) for row in rows]
