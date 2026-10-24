from typing import Any


class IngestionError(Exception):
    """A file was rejected.

    `code` is a stable machine-readable reason; `message` is safe to show to the client:
    it never contains file paths, document text or library internals.
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
