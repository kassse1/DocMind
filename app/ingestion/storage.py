"""Local filesystem storage for uploaded originals.

Paths are built only from server-generated values (a random temp name, the document
UUID), never from anything the client sent, so path traversal is impossible by design.
"""

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        # Temp files live under the same root so the final move is an atomic rename.
        self.temp_dir = root / "tmp"
        self.documents_dir = root / "documents"

    def new_temp_path(self) -> Path:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        return self.temp_dir / f"{uuid.uuid4().hex}.part"

    def path_for(self, document_id: uuid.UUID) -> Path:
        return self.documents_dir / document_id.hex

    def store(self, temp_path: Path, document_id: uuid.UUID) -> Path:
        destination = self.path_for(document_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, destination)
        return destination

    def delete(self, document_id: uuid.UUID) -> None:
        self.path_for(document_id).unlink(missing_ok=True)

    @staticmethod
    def remove_quietly(path: Path) -> None:
        """Best-effort cleanup that never masks the error being handled by the caller."""
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # Class only: the message would contain the server-side path.
            logger.warning("Could not remove a storage file (%s)", type(exc).__name__)
