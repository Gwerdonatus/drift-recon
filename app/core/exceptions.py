"""
Domain exceptions. Using custom exceptions instead of raw HTTPException
throughout service layer keeps business logic independent of transport layer.
FastAPI exception handlers in main.py translate these to HTTP responses.
"""

from __future__ import annotations


class ReconciliationError(Exception):
    """Base exception for all domain errors."""

    pass


class IngestionError(ReconciliationError):
    """CSV or data ingestion failed."""

    def __init__(self, message: str, row_index: int | None = None):
        self.row_index = row_index
        super().__init__(message)


class DuplicateIngestionError(ReconciliationError):
    """
    Idempotency violation: same data ingested twice.
    Callers should catch this and return 409 Conflict, not 500.
    """

    def __init__(self, external_id: str):
        self.external_id = external_id
        super().__init__(f"Record with external_id={external_id!r} already exists.")


class MatcherError(ReconciliationError):
    """Matching engine encountered an unrecoverable error."""

    pass


class DriftAnalysisError(ReconciliationError):
    """Drift analysis failed (e.g., insufficient data)."""

    pass


class InsufficientDataError(ReconciliationError):
    """Not enough historical data for statistical analysis."""

    def __init__(self, required: int, available: int):
        self.required = required
        self.available = available
        super().__init__(f"Need at least {required} data points, have {available}.")


class ResourceNotFoundError(ReconciliationError):
    """Generic 404-equivalent for domain objects."""

    def __init__(self, resource: str, identifier: str | int):
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} with id={identifier!r} not found.")
