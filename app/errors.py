"""Domain errors shared by the store and the HTTP layer."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_detail(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class Invalid(ServiceError):
    status_code = 422
