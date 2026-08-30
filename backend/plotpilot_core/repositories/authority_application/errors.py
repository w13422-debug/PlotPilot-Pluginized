from __future__ import annotations

from ..authority import ConflictError


class AuthorityApplicationError(ConflictError):
    """Typed domain failure consumed by the future thin Core HTTP adapter."""

    status_code: int
    error_code: str
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    @property
    def status(self) -> int:
        """Compatibility alias for adapters that expose ``status``."""
        return self.status_code

    def to_error(self) -> dict[str, object]:
        return {
            "schema": "core-http-error/v1",
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
        }


class CrossWorkspaceError(AuthorityApplicationError):
    status_code = 404
    error_code = "cross_workspace"


class StaleCasError(AuthorityApplicationError):
    status_code = 409
    error_code = "stale_cas"


class OperationKeyReuseError(AuthorityApplicationError):
    status_code = 409
    error_code = "operation_key_reuse"


class IncompletePublicationError(AuthorityApplicationError):
    status_code = 422
    error_code = "incomplete_publication"


class UnknownReferenceError(IncompletePublicationError):
    """A missing durable reference, also considered incomplete authority."""

    status_code = 404
    error_code = "unknown_reference"


__all__ = [
    "AuthorityApplicationError",
    "CrossWorkspaceError",
    "IncompletePublicationError",
    "OperationKeyReuseError",
    "StaleCasError",
    "UnknownReferenceError",
]
