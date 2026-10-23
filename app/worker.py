import asyncio
import logging
import signal
from contextlib import suppress

from app.config import Settings
from app.db import make_pool
from app.logging_config import configure_logging
from app.store import Store

logger = logging.getLogger(__name__)


async def heartbeat(store: Store, job: dict, settings: Settings, lost: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(settings.lease_seconds / 3)
        try:
            if not await store.renew(job, settings.lease_seconds):
                lost.set()
                logger.warning("lease_lost", extra={"transaction_id": job["transaction_id"]})
                return
        except Exception:
            # A transient DB outage may recover before expiration. Completion is fenced anyway.
            logger.exception("heartbeat_failed", extra={"transaction_id": job["transaction_id"]})


async def process_job(store: Store, job: dict, settings: Settings) -> None:
    fields = {"transaction_id": job["transaction_id"], "attempt": job["attempts"]}
    logger.info("processing_started", extra=fields)
    lost = asyncio.Event()
    renewal = asyncio.create_task(heartbeat(store, job, settings, lost))
    try:
        # Simulates slow external I/O without holding a DB connection or blocking the API.
        await asyncio.sleep(settings.processing_delay_seconds)
        if not lost.is_set() and await store.complete(job):
            logger.info("processing_completed", extra=fields)
        else:
            logger.warning("completion_skipped_stale_lease", extra=fields)
    except asyncio.CancelledError:
        # Graceful shutdown: release immediately. Hard crashes recover after lease expiration.
        with suppress(Exception):
            await store.retry(job, "Worker shut down before completion", delay=0)
        raise
    except Exception as exc:
        logger.exception("processing_failed", extra=fields)
        # Retry indefinitely with bounded exponential backoff; never silently discard a job.
        delay = min(2 ** min(job["attempts"], 8), 60)
        with suppress(Exception):
            await store.retry(job, type(exc).__name__, delay=delay)
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal


async def worker_slot(store: Store, settings: Settings, claim_lock: asyncio.Lock) -> None:
    while True:
        try:
            # Share one idle poll across slots; processing remains concurrent. This
            # avoids N identical empty queries per interval on small cloud instances.
            async with claim_lock:
                job = await store.claim(settings.lease_seconds)
                if job is None:
                    await asyncio.sleep(settings.poll_interval_seconds)
            if job is not None:
                await process_job(store, job, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker_iteration_failed")
            await asyncio.sleep(1)


async def run() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    pool = make_pool(settings)
    await pool.open(wait=True, timeout=10)
    claim_lock = asyncio.Lock()
    slots = [
        asyncio.create_task(worker_slot(Store(pool), settings, claim_lock))
        for _ in range(settings.worker_concurrency)
    ]
    logger.info(
        "worker_ready concurrency=%s delay=%ss",
        settings.worker_concurrency,
        settings.processing_delay_seconds,
    )
    try:
        await stopped.wait()
    finally:
        for slot in slots:
            slot.cancel()
        await asyncio.gather(*slots, return_exceptions=True)
        await pool.close()
        logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
