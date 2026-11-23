from __future__ import annotations

import uuid
from pathlib import Path

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    monkeypatch.setenv(
        "SUBTITLE_DB_PATH", str(tmp_path / f"test-{uuid.uuid4().hex}.db")
    )
    db.init_db()
    with TestClient(app) as test_client:
        yield test_client
