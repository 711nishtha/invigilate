"""Delete proctoring evidence whose retention window has closed.

Retention is written on every evidence row at capture time, from the quiz's
policy. This job is what makes that promise true rather than decorative: without
it, "kept for 30 days" means "kept forever, with a date attached".

Run it on a schedule — a Render cron job, or any daily task runner:

    python -m scripts.purge_evidence            # delete what is due
    python -m scripts.purge_evidence --dry-run  # report only
    python -m scripts.purge_evidence --limit 2000

Deliberate design choices:

* **The object is deleted before the row.** If the process dies between the two,
  the row remains and is retried tomorrow — an orphaned row is recoverable, an
  orphaned object in a bucket nobody tracks is not.
* **A storage failure is not fatal.** One unreachable object must not stop the
  rest of the batch, so each is attempted independently and failures are counted
  and reported.
* **Exit codes are meaningful**, so a cron wrapper can alert:
  0 everything due was deleted (or nothing was due), 1 some deletions failed,
  2 the job could not run at all.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.logging import Event, configure_logging, get_logger, log_event
from app.db.session import SessionLocal
from app.services import evidence_service
from app.storage.base import StorageError
from app.storage.factory import get_storage_provider

logger = get_logger("securequiz.purge")


async def purge(*, limit: int, dry_run: bool) -> int:
    db = SessionLocal()
    storage = get_storage_provider()
    deleted = 0
    failed = 0
    try:
        expired = evidence_service.expired_evidence(db, limit=limit)
        if not expired:
            logger.info("Nothing is due for deletion.")
            return 0

        logger.info("%d evidence object(s) are past their retention date.", len(expired))
        if dry_run:
            # The path is deliberately not printed: it is the only secret
            # protecting the object, and this output often lands in a log.
            for row in expired:
                logger.info(
                    "would delete %s (%s, captured %s)",
                    row.id,
                    row.kind.value,
                    row.captured_at.isoformat(),
                )
            return 0

        for row in expired:
            try:
                # Object first: an orphaned row is recoverable, an untracked
                # object is not.
                await storage.delete(bucket=row.bucket, path=row.object_path)
            except StorageError as exc:
                failed += 1
                logger.warning("could not delete object for %s: %s", row.id, exc.message)
                continue

            db.delete(row)
            deleted += 1

        db.commit()
        log_event(Event.EVIDENCE_PURGED, deleted=deleted, failed=failed)
        logger.info("Deleted %d object(s); %d failure(s).", deleted, failed)
        return 1 if failed else 0
    finally:
        await storage.aclose()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500, help="maximum rows per run")
    parser.add_argument("--dry-run", action="store_true", help="report without deleting")
    args = parser.parse_args()

    configure_logging()
    try:
        return asyncio.run(purge(limit=args.limit, dry_run=args.dry_run))
    except Exception as exc:  # pragma: no cover - operational entry point
        logger.error("purge could not run: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
