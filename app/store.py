from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from app.models import TransactionInput


class ConflictingTransaction(Exception):
    """An existing ID was reused with different transaction details."""


class Store:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def accept(self, transaction: TransactionInput) -> bool:
        """Return True for an identical duplicate; commit before acknowledging."""
        async with self.pool.connection() as connection:
            await connection.execute("SET LOCAL statement_timeout = '200ms'")
            await connection.execute("SET LOCAL lock_timeout = '150ms'")
            cursor = await connection.execute(
                """
                INSERT INTO transactions
                    (transaction_id, source_account, destination_account, amount, currency)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (transaction_id) DO NOTHING
                RETURNING transaction_id
                """,
                (
                    transaction.transaction_id,
                    transaction.source_account,
                    transaction.destination_account,
                    transaction.amount,
                    transaction.currency,
                ),
            )
            inserted = await cursor.fetchone()
            if inserted:
                await connection.execute(
                    "INSERT INTO jobs (transaction_id) VALUES (%s)", (transaction.transaction_id,)
                )
                duplicate = False
            else:
                cursor = await connection.execute(
                    "SELECT source_account, destination_account, amount, currency "
                    "FROM transactions WHERE transaction_id = %s",
                    (transaction.transaction_id,),
                )
                existing = await cursor.fetchone()
                if existing != transaction.model_dump(exclude={"transaction_id"}):
                    raise ConflictingTransaction
                duplicate = True
        # Exiting connection() commits BOTH records; a failed commit never returns 202.
        return duplicate

    async def get(self, transaction_id: str) -> dict | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = %s", (transaction_id,)
            )
            return await cursor.fetchone()

    async def healthy(self) -> None:
        async with self.pool.connection() as connection:
            await connection.execute("SELECT 1 FROM transactions LIMIT 1")

    async def claim(self, lease_seconds: float) -> dict | None:
        # Claim + lease happen under a brief row lock, never during the 30-second wait.
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                WITH candidate AS (
                    SELECT transaction_id FROM jobs
                    WHERE completed_at IS NULL
                      AND available_at <= clock_timestamp()
                      AND (leased_until IS NULL OR leased_until <= clock_timestamp())
                    ORDER BY available_at, transaction_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE jobs AS j
                SET lease_token = %s,
                    leased_until = clock_timestamp() + %s * interval '1 second',
                    attempts = j.attempts + 1
                FROM candidate AS c
                WHERE j.transaction_id = c.transaction_id
                RETURNING j.transaction_id, j.lease_token, j.attempts
                """,
                (uuid4(), lease_seconds),
            )
            return await cursor.fetchone()

    async def renew(self, job: dict, lease_seconds: float) -> bool:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE jobs
                SET leased_until = clock_timestamp() + %s * interval '1 second'
                WHERE transaction_id = %s AND lease_token = %s
                  AND leased_until > clock_timestamp() AND completed_at IS NULL
                RETURNING transaction_id
                """,
                (lease_seconds, job["transaction_id"], job["lease_token"]),
            )
            return await cursor.fetchone() is not None

    async def complete(self, job: dict) -> bool:
        async with self.pool.connection() as connection:
            # A token and an unexpired lease fence out stale workers after a retry.
            cursor = await connection.execute(
                """
                UPDATE jobs
                SET completed_at = clock_timestamp(), lease_token = NULL,
                    leased_until = NULL, last_error = NULL
                WHERE transaction_id = %s AND lease_token = %s
                  AND leased_until > clock_timestamp() AND completed_at IS NULL
                RETURNING completed_at
                """,
                (job["transaction_id"], job["lease_token"]),
            )
            completed = await cursor.fetchone()
            if completed is None:
                return False
            await connection.execute(
                "UPDATE transactions SET status = 'PROCESSED', processed_at = %s "
                "WHERE transaction_id = %s AND status = 'PROCESSING'",
                (completed["completed_at"], job["transaction_id"]),
            )
        return True

    async def retry(self, job: dict, error: str, delay: float) -> bool:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE jobs
                SET lease_token = NULL, leased_until = NULL, last_error = %s,
                    available_at = clock_timestamp() + %s * interval '1 second'
                WHERE transaction_id = %s AND lease_token = %s AND completed_at IS NULL
                RETURNING transaction_id
                """,
                (error[:500], delay, job["transaction_id"], job["lease_token"]),
            )
            return await cursor.fetchone() is not None
