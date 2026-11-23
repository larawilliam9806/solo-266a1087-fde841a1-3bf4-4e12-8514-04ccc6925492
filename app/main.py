"""Subtitle delivery API.

Run with ``uvicorn app.main:app``.  See README.md for the full workflow.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import db, store
from .errors import NotFound, ServiceError
from .schemas import (
    EditTableIn,
    FilmCreate,
    SourceReelCreate,
    SubtitleDocumentIn,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.check_writable()
    yield


app = FastAPI(
    title="纪录片字幕交付 API",
    version="1.0.0",
    description=(
        "把原片已审台词按剪辑表落到成片时码上，发布后供播出端查询。"
        "所有区间均为左闭右开：起点计入，终点不计入。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.to_detail()})


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "request body failed validation",
                "details": [
                    {"loc": list(item["loc"]), "msg": item["msg"]}
                    for item in exc.errors()
                ],
            }
        },
    )


@app.exception_handler(sqlite3.IntegrityError)
async def _integrity_error(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "integrity_error",
                "message": str(exc),
            }
        },
    )


# ---------------------------------------------------------------------------
# source reels & subtitles
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/source-reels", status_code=201)
def create_source_reel(body: SourceReelCreate):
    with db.write_conn() as conn:
        return store.create_source_reel(conn, body.name)


@app.get("/source-reels")
def list_source_reels():
    with db.read_conn() as conn:
        return store.list_source_reels(conn)


@app.get("/source-reels/{reel_id}")
def get_source_reel(reel_id: int):
    with db.read_conn() as conn:
        return store.get_source_reel(conn, reel_id)


@app.post("/source-reels/{reel_id}/subtitles", status_code=201)
def create_subtitle_version(reel_id: int, body: SubtitleDocumentIn):
    with db.write_conn() as conn:
        return store.create_subtitle_version(
            conn, reel_id, [line.model_dump() for line in body.lines]
        )


@app.get("/source-reels/{reel_id}/subtitles")
def list_subtitle_versions(reel_id: int):
    with db.read_conn() as conn:
        return store.list_subtitle_versions(conn, reel_id)


@app.get("/source-reels/{reel_id}/subtitles/latest")
def get_latest_subtitle(reel_id: int):
    with db.read_conn() as conn:
        document = store.get_subtitle_document(conn, reel_id)
        if document is None:
            raise NotFound(
                "no_subtitles",
                f"source reel {reel_id} has no subtitle version",
                details={"source_reel_id": reel_id},
            )
        return document


@app.get("/source-reels/{reel_id}/subtitles/{version}")
def get_subtitle_version(reel_id: int, version: int):
    with db.read_conn() as conn:
        return store.get_subtitle_document(conn, reel_id, version)


# ---------------------------------------------------------------------------
# films & edit tables
# ---------------------------------------------------------------------------


@app.post("/films", status_code=201)
def create_film(body: FilmCreate):
    with db.write_conn() as conn:
        return store.create_film(conn, body.name)


@app.get("/films")
def list_films():
    with db.read_conn() as conn:
        return store.list_films(conn)


@app.get("/films/{film_id}")
def get_film(film_id: int):
    with db.read_conn() as conn:
        return store.get_film(conn, film_id)


@app.post("/films/{film_id}/edit-tables", status_code=201)
def create_edit_table(film_id: int, body: EditTableIn):
    with db.write_conn() as conn:
        return store.create_edit_table(
            conn, film_id, [segment.model_dump() for segment in body.segments]
        )


@app.get("/films/{film_id}/edit-tables")
def list_edit_versions(film_id: int):
    with db.read_conn() as conn:
        return store.list_edit_versions(conn, film_id)


@app.get("/films/{film_id}/edit-tables/latest")
def get_latest_edit_table(film_id: int):
    with db.read_conn() as conn:
        table = store.get_edit_table(conn, film_id)
        if table is None:
            raise NotFound(
                "no_edit_table",
                f"film {film_id} has no edit table",
                details={"film_id": film_id},
            )
        return table


@app.get("/films/{film_id}/edit-tables/{version}")
def get_edit_table(film_id: int, version: int):
    with db.read_conn() as conn:
        return store.get_edit_table(conn, film_id, version)


# ---------------------------------------------------------------------------
# deliveries
# ---------------------------------------------------------------------------


@app.post("/films/{film_id}/deliveries", status_code=201)
def create_delivery(film_id: int):
    """Start a delivery against the film's current edit table and subtitles."""

    with db.write_conn() as conn:
        return store.create_delivery(conn, film_id)


@app.get("/films/{film_id}/deliveries")
def list_deliveries(film_id: int):
    with db.read_conn() as conn:
        return store.list_deliveries(conn, film_id)


@app.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: int):
    with db.read_conn() as conn:
        return store.delivery_detail(conn, delivery_id)


@app.get("/deliveries/{delivery_id}/preview")
def preview_delivery(delivery_id: int):
    with db.read_conn() as conn:
        return store.delivery_preview(conn, delivery_id)


@app.post("/deliveries/{delivery_id}/publish")
def publish_delivery(delivery_id: int):
    with db.write_conn() as conn:
        published, payload = store.publish_delivery(conn, delivery_id)

    if not published:
        return JSONResponse(status_code=409, content={"error": payload})
    return payload


# ---------------------------------------------------------------------------
# broadcast side
# ---------------------------------------------------------------------------


@app.get("/films/{film_id}/publications")
def list_publications(film_id: int):
    with db.read_conn() as conn:
        return store.list_publications(conn, film_id)


@app.get("/publications/{publication_id}")
def get_publication(publication_id: int):
    with db.read_conn() as conn:
        return store.get_publication(conn, publication_id)


@app.get("/films/{film_id}/latest-publication")
def latest_publication(film_id: int, output_version: int | None = None):
    with db.read_conn() as conn:
        return store.latest_publication(conn, film_id, output_version)


@app.get("/films/{film_id}/subtitles/latest")
def latest_film_subtitles(film_id: int, output_version: int | None = None):
    """Latest successful delivery: finished-film subtitles + review items."""

    with db.read_conn() as conn:
        publication = store.latest_publication(conn, film_id, output_version)
    return {
        "film_id": publication["film_id"],
        "film_name": publication["film_name"],
        "output_version": publication["output_version"],
        "publication_id": publication["id"],
        "edit_version": publication["edit_version"],
        "published_at": publication["created_at"],
        "subtitles": publication["subtitles"],
        "reviews": publication["reviews"],
    }
