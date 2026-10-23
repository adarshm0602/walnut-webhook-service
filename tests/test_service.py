import asyncio
from datetime import datetime
from decimal import Decimal
from time import perf_counter

import pytest
from psycopg import Error as DatabaseError

from app.models import TransactionInput
from app.worker import process_job, worker_slot
from tests.conftest import payload


async def counts(store):
    async with store.pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT (SELECT count(*) FROM transactions) AS transactions, "
            "(SELECT count(*) FROM jobs) AS jobs"
        )
        return await cursor.fetchone()


async def test_health_and_unknown_id(context):
    _, _, client = context
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "HEALTHY"
    assert response.json()["current_time"].endswith("Z")
    assert (await client.get("/v1/transactions/missing")).status_code == 404


async def test_accept_then_process_and_preserve_decimal(context):
    settings, store, client = context
    data = payload(amount=150.50, currency="usd")
    response = await client.post("/v1/webhooks/transactions", json=data)
    assert response.status_code == 202 and not response.json()["duplicate"]
    response = await client.get("/v1/transactions/txn_test_001")
    before = response.json()[0]
    assert before["status"] == "PROCESSING" and before["processed_at"] is None
    assert before["currency"] == "USD"
    job = await store.claim(settings.lease_seconds)
    await process_job(store, job, settings)
    response = await client.get("/v1/transactions/txn_test_001")
    after = response.json()[0]
    assert after["status"] == "PROCESSED"
    assert after["amount"] == 150.50
    assert datetime.fromisoformat(after["processed_at"]) >= datetime.fromisoformat(
        after["created_at"]
    )
    assert (await store.get("txn_test_001"))["amount"] == Decimal("150.50")
    assert await store.claim(settings.lease_seconds) is None


async def test_simultaneous_duplicates_and_duplicate_after_completion(context):
    settings, store, client = context
    responses = await asyncio.gather(
        *[client.post("/v1/webhooks/transactions", json=payload()) for _ in range(40)]
    )
    assert all(response.status_code == 202 for response in responses)
    assert sum(not response.json()["duplicate"] for response in responses) == 1
    assert await counts(store) == {"transactions": 1, "jobs": 1}
    job = await store.claim(settings.lease_seconds)
    await process_job(store, job, settings)
    original = await store.get("txn_test_001")
    duplicate = await client.post("/v1/webhooks/transactions", json=payload())
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"]
    assert await store.get("txn_test_001") == original
    assert await store.claim(settings.lease_seconds) is None


async def test_conflicting_duplicate_does_not_overwrite(context):
    _, store, client = context
    await client.post("/v1/webhooks/transactions", json=payload())
    response = await client.post("/v1/webhooks/transactions", json=payload(amount=999))
    assert response.status_code == 409
    assert (await store.get("txn_test_001"))["amount"] == Decimal("1500")
    assert await counts(store) == {"transactions": 1, "jobs": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"amount": -1},
        {"amount": 0},
        {"amount": 1.001},
        {"amount": "NaN"},
        {"amount": 10000000000000000},
        {"currency": "INVALID"},
        {"transaction_id": ""},
        {"source_account": "bad account"},
        {"extra": "unexpected"},
    ],
)
async def test_validation(context, change):
    _, store, client = context
    response = await client.post("/v1/webhooks/transactions", json=payload(**change))
    assert response.status_code == 422
    assert await counts(store) == {"transactions": 0, "jobs": 0}


async def test_acceptance_rolls_back_when_job_insert_fails(context):
    _, store, client = context
    async with store.pool.connection() as connection:
        await connection.execute("ALTER TABLE jobs ADD CHECK (false) NOT VALID")
    response = await client.post("/v1/webhooks/transactions", json=payload())
    assert response.status_code == 503 and response.headers["retry-after"] == "1"
    assert await counts(store) == {"transactions": 0, "jobs": 0}


async def test_concurrent_workers_claim_each_job_once(context):
    settings, store, _ = context
    for index in range(12):
        await store.accept(TransactionInput(**payload(f"txn_claim_{index}")))
    jobs = await asyncio.gather(*[store.claim(settings.lease_seconds) for _ in range(20)])
    claimed = [job for job in jobs if job]
    assert len(claimed) == 12
    assert len({job["transaction_id"] for job in claimed}) == 12
    assert all(job["attempts"] == 1 for job in claimed)


async def test_expired_job_is_recovered_and_old_worker_cannot_complete(context):
    settings, store, client = context
    await client.post("/v1/webhooks/transactions", json=payload())
    abandoned = await store.claim(settings.lease_seconds)
    assert await store.claim(settings.lease_seconds) is None
    # Represents a killed worker that cannot heartbeat or release its lease.
    async with store.pool.connection() as connection:
        await connection.execute("UPDATE jobs SET leased_until = now() - interval '1 second'")
    assert not await store.complete(abandoned)
    recovered = await store.claim(settings.lease_seconds)
    assert recovered["attempts"] == 2 and recovered["lease_token"] != abandoned["lease_token"]
    assert not await store.complete(abandoned)
    assert not await store.retry(abandoned, "stale", 0)
    await process_job(store, recovered, settings)
    assert (await store.get("txn_test_001"))["status"] == "PROCESSED"
    assert await counts(store) == {"transactions": 1, "jobs": 1}


async def test_heartbeat_extends_lease_and_graceful_cancel_releases_job(context):
    settings, store, client = context
    settings = settings.model_copy(update={"processing_delay_seconds": 3, "lease_seconds": 0.3})
    await client.post("/v1/webhooks/transactions", json=payload())
    job = await store.claim(settings.lease_seconds)
    task = asyncio.create_task(process_job(store, job, settings))
    try:
        await asyncio.sleep(0.7)
        assert await store.claim(settings.lease_seconds) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    recovered = await store.claim(settings.lease_seconds)
    assert recovered and recovered["attempts"] == 2


async def test_failure_retries_without_losing_job(context, monkeypatch):
    settings, store, client = context
    await client.post("/v1/webhooks/transactions", json=payload())
    original_complete = store.complete

    async def fail_completion(job):
        raise DatabaseError("simulated database failure")

    monkeypatch.setattr(store, "complete", fail_completion)
    job = await store.claim(settings.lease_seconds)
    await process_job(store, job, settings)
    assert (await store.get("txn_test_001"))["status"] == "PROCESSING"
    assert await store.claim(settings.lease_seconds) is None  # backoff applies
    async with store.pool.connection() as connection:
        await connection.execute("UPDATE jobs SET available_at = now()")
    monkeypatch.setattr(store, "complete", original_complete)
    recovered = await store.claim(settings.lease_seconds)
    assert recovered["attempts"] == 2
    await process_job(store, recovered, settings)
    assert (await store.get("txn_test_001"))["status"] == "PROCESSED"


async def test_missing_schema_is_unhealthy_and_never_acknowledged(context):
    _, store, client = context
    async with store.pool.connection() as connection:
        await connection.execute("ALTER TABLE transactions RENAME TO unavailable_transactions")
    assert (await client.get("/")).status_code == 503
    assert (await client.post("/v1/webhooks/transactions", json=payload())).status_code == 503


async def test_exact_amount_serialization_without_float_round_trip(context):
    _, _, client = context
    await client.post("/v1/webhooks/transactions", json=payload(amount="9999999999999999.99"))
    response = await client.get("/v1/transactions/txn_test_001")
    assert '"amount":9999999999999999.99' in response.text


@pytest.mark.slow
async def test_real_30_second_processing_and_fast_acknowledgments_under_load(context):
    settings, store, client = context
    settings = settings.model_copy(update={"processing_delay_seconds": 30, "lease_seconds": 60})
    slots = [asyncio.create_task(worker_slot(store, settings)) for _ in range(20)]

    async def send(index):
        started = perf_counter()
        response = await client.post("/v1/webhooks/transactions", json=payload(f"txn_load_{index}"))
        return response, perf_counter() - started

    try:
        responses = await asyncio.gather(*[send(index) for index in range(20)])
        assert all(response.status_code == 202 for response, _ in responses)
        assert max(elapsed for _, elapsed in responses) < 0.5
        assert (await store.get("txn_load_0"))["status"] == "PROCESSING"
        deadline = perf_counter() + 40
        while perf_counter() < deadline:
            async with store.pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT count(*) AS count FROM transactions WHERE status = 'PROCESSED'"
                )
                if (await cursor.fetchone())["count"] == 20:
                    break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("Transactions did not all complete within 40 seconds")
        async with store.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT min(extract(epoch FROM processed_at - created_at)) AS minimum, "
                "max(extract(epoch FROM processed_at - created_at)) AS maximum FROM transactions"
            )
            timings = await cursor.fetchone()
        assert timings["minimum"] >= 30 and timings["maximum"] < 40
        max_ack_ms = max(t for _, t in responses) * 1000
        print(
            f"20 concurrent requests: max acknowledgment={max_ack_ms:.1f}ms; "
            f"processing range={timings['minimum']:.2f}–{timings['maximum']:.2f}s"
        )
    finally:
        for slot in slots:
            slot.cancel()
        await asyncio.gather(*slots, return_exceptions=True)
