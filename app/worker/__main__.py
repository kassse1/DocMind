"""Indexing worker CLI.

    python -m app.worker                  # process jobs until stopped (Ctrl+C)
    python -m app.worker --once           # process every runnable job, then exit
    python -m app.worker reindex <id>     # queue a document for re-indexing
    python -m app.worker healthcheck      # exit 0 if the heartbeat file is fresh (containers)

Several workers can run at once: FOR UPDATE SKIP LOCKED hands each job to one of them.
A worker killed mid-job leaves the job "running"; it is reclaimed after the lease expires.
SIGTERM (docker stop) lets the job in progress finish, then exits.

Startup refuses a database whose schema is not at the Alembic head of this code (exit 3):
migrations are a separate one-shot step, never run by workers.
"""

import argparse
import logging
import signal
import threading
import time
import uuid
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging import configure_logging
from app.core.schema import expected_head, schema_is_current
from app.embeddings.factory import create_embedder
from app.ingestion.storage import FileStorage
from app.models import EMBEDDING_DIMENSION, Document
from app.services import jobs, maintenance
from app.services.indexing import DocumentIndexer

logger = logging.getLogger("app.worker")


SCHEMA_MISMATCH_EXIT = 3


class SchemaMismatchError(RuntimeError):
    pass


def _heartbeat(path: Path, stop: threading.Event, interval_s: float = 5.0) -> None:
    """Touch the file while the worker loop is alive. Process liveness, not job progress:
    a job stuck in a single document is handled by the job lease, not by this."""
    while not stop.is_set():
        try:
            path.touch()
        except OSError as exc:
            logger.warning("Heartbeat file not writable (%s)", type(exc).__name__)
        stop.wait(interval_s)


def run(settings: Settings, *, once: bool) -> None:
    engine = create_db_engine(settings)
    try:
        with engine.connect() as conn:
            if not schema_is_current(conn):
                raise SchemaMismatchError(
                    f"database schema is not at {expected_head()}; run the migrations first"
                )
        embedder = create_embedder(settings, EMBEDDING_DIMENSION)
        # Load the model (and download it on first use) before claiming any job, so a
        # broken model setup fails here instead of burning job attempts.
        logger.info(
            "Loading embedding model %s (max %d input tokens)",
            embedder.model_name, embedder.max_input_tokens,
        )
        indexer = DocumentIndexer(
            create_session_factory(engine), FileStorage(settings.storage_dir), embedder, settings
        )

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        if settings.worker_heartbeat_file is not None:
            threading.Thread(
                target=_heartbeat, args=(settings.worker_heartbeat_file, stop), daemon=True
            ).start()
        session_factory = create_session_factory(engine)
        last_maintenance = 0.0
        logger.info("Worker started (once=%s)", once)
        while not stop.is_set():
            if indexer.run_once():
                continue
            if time.monotonic() - last_maintenance >= settings.maintenance_interval_s:
                with session_factory() as session:
                    maintenance.prune(session)
                last_maintenance = time.monotonic()
            if once:
                break
            stop.wait(settings.worker_poll_interval_s)
        logger.info("Worker stopped")
    finally:
        engine.dispose()


def reindex(settings: Settings, document_id: uuid.UUID) -> int:
    engine = create_db_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session, session.begin():
            if session.get(Document, document_id) is None:
                print(f"Document {document_id} not found")
                return 1
            queued = jobs.request_reindex(session, document_id)
        print(f"Re-index queued for {document_id}" if queued else "A job is already queued")
        return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.worker", description="Indexing worker")
    parser.add_argument("--once", action="store_true", help="exit when no job is runnable")
    commands = parser.add_subparsers(dest="command")
    reindex_parser = commands.add_parser("reindex", help="queue a document for re-indexing")
    reindex_parser.add_argument("document_id", type=uuid.UUID)
    health_parser = commands.add_parser("healthcheck", help="check the heartbeat file age")
    health_parser.add_argument("--max-age", type=float, default=60.0, help="seconds")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format, "worker")
    if args.command == "healthcheck":
        return healthcheck(settings, args.max_age)
    if args.command == "reindex":
        return reindex(settings, args.document_id)
    try:
        run(settings, once=args.once)
    except KeyboardInterrupt:
        logger.info("Interrupted; a job in progress will be retried after its lease expires")
    except SchemaMismatchError as exc:
        logger.error("Worker not started: %s", exc)
        return SCHEMA_MISMATCH_EXIT
    return 0


def healthcheck(settings: Settings, max_age_s: float) -> int:
    path = settings.worker_heartbeat_file
    if path is None:
        print("DOCMIND_WORKER_HEARTBEAT_FILE is not set")
        return 1
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        print("no heartbeat yet")
        return 1
    if age > max_age_s:
        print(f"heartbeat is {age:.0f}s old")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
