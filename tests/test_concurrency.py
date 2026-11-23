from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from app import db, store
from app.errors import Conflict


def _fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / f"{uuid.uuid4().hex}.db"
    db.init_db(path)
    return path


def _seed(path: Path) -> tuple[int, int]:
    with db.write_conn(path) as conn:
        reel = store.create_source_reel(conn, "r")
        store.create_subtitle_version(
            conn,
            reel["id"],
            [{"number": 1, "start_ms": 0, "end_ms": 1_000, "text": "x"}],
        )
        film = store.create_film(conn, "f")
        store.create_edit_table(
            conn,
            film["id"],
            [
                {
                    "source_reel_id": reel["id"],
                    "source_start_ms": 0,
                    "source_end_ms": 1_000,
                    "output_start_ms": 0,
                }
            ],
        )
        return reel["id"], film["id"]


def test_concurrent_subtitle_versions_get_unique_numbers(tmp_path):
    path = _fresh_db(tmp_path)
    reel_id, _ = _seed(path)

    results: list[int] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            with db.write_conn(path) as conn:
                document = store.create_subtitle_version(conn, reel_id, [])
            results.append(document["version"])
        except BaseException as exc:  # pragma: no cover - diagnostic
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, failures
    assert sorted(results) == list(range(2, 10))


def test_concurrent_publish_yields_distinct_output_versions(tmp_path):
    path = _fresh_db(tmp_path)
    _, film_id = _seed(path)

    with db.write_conn(path) as conn:
        delivery_ids = [
            store.create_delivery(conn, film_id)["id"] for _ in range(6)
        ]

    published: list[int] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(len(delivery_ids))

    def worker(delivery_id: int) -> None:
        try:
            barrier.wait()
            with db.write_conn(path) as conn:
                ok, payload = store.publish_delivery(conn, delivery_id)
            assert ok
            published.append(payload["output_version"])
        except BaseException as exc:  # pragma: no cover - diagnostic
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(d,)) for d in delivery_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, failures
    assert sorted(published) == list(range(1, len(delivery_ids) + 1))

    with db.read_conn(path) as conn:
        latest = store.latest_publication(conn, film_id)
    assert latest["output_version"] == len(delivery_ids)
