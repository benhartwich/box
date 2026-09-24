"""Domain errors, mapped to HTTP responses by the API layers."""

from __future__ import annotations


class DomainError(Exception):
    """Base class. ``message`` is safe to show to users (German UI texts)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    def __init__(self, message: str = "Nicht gefunden.") -> None:
        super().__init__(message)


class ConflictError(DomainError):
    pass


class InvalidInputError(DomainError):
    pass
