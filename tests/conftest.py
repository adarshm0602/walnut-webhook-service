import asyncio
import os
from uuid import uuid4

import httpx
import psycopg
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.config import Settings
from app.db import make_pool, migrate
from app.main import create_app
from app.store import Store


@pytest_asyncio.fixture
async def context():
    # Every test gets a private schema; never truncate the running application's tables.
    base_url = os.environ.get("TEST_DATABASE_URL", Settings().database_url)
    schema = f"test_{uuid4().hex}"
    admin = await psycopg.AsyncConnection.connect(base_url, autocommit=True)
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database_url = make_conninfo(base_url, options=f"-csearch_path={schema}")
    settings = Settings(
        database_url=database_url,
        processing_delay_seconds=0.1,
        lease_seconds=0.6,
        poll_interval_seconds=0.01,
        worker_concurrency=4,
        log_level="WARNING",
    )
    pool = make_pool(settings)
    try:
        await asyncio.to_thread(migrate, database_url)
        await pool.open(wait=True, timeout=10)
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield settings, Store(pool), client
    finally:
        await pool.close()
        await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await admin.close()


def payload(transaction_id="txn_test_001", **changes):
    return {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
        **changes,
    }
