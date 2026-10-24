import uuid
from pathlib import Path

import pytest

from app.ingestion.storage import FileStorage


def test_paths_are_server_generated_and_inside_root(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    document_id = uuid.uuid4()

    assert storage.path_for(document_id) == tmp_path / "documents" / document_id.hex
    temp = storage.new_temp_path()
    assert temp.parent == tmp_path / "tmp"
    assert temp.name.endswith(".part")


def test_store_moves_temp_file_and_delete_removes_it(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    document_id = uuid.uuid4()
    temp = storage.new_temp_path()
    temp.write_bytes(b"data")

    stored = storage.store(temp, document_id)

    assert not temp.exists()
    assert stored.read_bytes() == b"data"
    storage.delete(document_id)
    assert not stored.exists()


def test_remove_quietly_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    FileStorage.remove_quietly(directory)  # unlink() on a directory raises OSError
    FileStorage.remove_quietly(tmp_path / "missing")

    assert directory.exists()
    assert str(tmp_path) not in caplog.text
