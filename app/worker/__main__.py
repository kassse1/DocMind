"""Indexing worker CLI.

    python -m app.worker                  # process jobs until stopped (Ctrl+C)
    python -m app.worker --once           # process every runnable job, then exit
    python -m app.worker reindex <id>     # queue a document for re-indexing

Several workers can run at once: FOR UPDATE SKIP LOCKED hands each job to one of them.
A worker killed mid-job leaves the job "running"; it is reclaimed after the lease expires.
"""

import argparse
import logging
import signal
import threading
import uuid

from app.core.config import Settings, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging import configure_logging
from app.embeddings.factory import create_embedder
from app.ingestion.storage import FileStorage
from app.models import EMBEDDING_DIMENSION, Document
from app.services import jobs
from app.services.indexing import DocumentIndexer

logger = logging.getLogger("app.worker")


def run(settings: Settings, *, once: bool) -> None:
    engine = create_db_engine(settings)
    try:
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
        logger.info("Worker started (once=%s)", once)
        while not stop.is_set():
            if indexer.run_once():
                continue
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
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format, "worker")
    if args.command == "reindex":
        return reindex(settings, args.document_id)
    try:
        run(settings, once=args.once)
    except KeyboardInterrupt:
        logger.info("Interrupted; a job in progress will be retried after its lease expires")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
