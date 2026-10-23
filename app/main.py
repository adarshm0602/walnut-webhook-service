import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

import simplejson
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from app.config import Settings
from app.db import make_pool
from app.logging_config import configure_logging
from app.models import Acknowledgment, HealthOutput, TransactionInput, TransactionOutput
from app.store import ConflictingTransaction, Store

logger = logging.getLogger(__name__)


class DecimalJSONResponse(JSONResponse):
    def render(self, content: object) -> bytes:
        def encode(value: object) -> str:
            if isinstance(value, datetime):
                return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
            raise TypeError(f"Cannot encode {type(value)}")

        return simplejson.dumps(
            content, use_decimal=True, allow_nan=False, default=encode, separators=(",", ":")
        ).encode("utf-8")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging(settings.log_level)
        pool = make_pool(settings)
        await pool.open(wait=True, timeout=10)
        application.state.store = Store(pool)
        try:
            yield
        finally:
            await pool.close()

    application = FastAPI(
        title="Walnut Transactions API",
        version="1.0.0",
        description=(
            "Accept a transaction immediately, then process it durably in a separate worker. "
            "Use a new transaction_id for each test. "
            "Repeat an identical request to test idempotency. "
            "GET responses are single-item arrays, matching the assessment."
        ),
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def request_metrics(request: Request, call_next):
        started = perf_counter()
        request_id = uuid4().hex
        response = await call_next(request)
        duration = round((perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = str(duration)
        logger.info(
            "%s %s",
            request.method,
            request.url.path,
            extra={
                "request_id": request_id,
                "http_status": response.status_code,
                "duration_ms": duration,
            },
        )
        return response

    def unavailable() -> HTTPException:
        return HTTPException(
            status_code=503,
            detail=(
                "Storage temporarily unavailable; retry this request with the same transaction_id."
            ),
            headers={"Retry-After": "1"},
        )

    @application.get("/", response_model=HealthOutput, tags=["Health"])
    async def health(request: Request):
        healthy = True
        try:
            async with asyncio.timeout(0.4):
                await request.app.state.store.healthy()
        except (DatabaseError, PoolTimeout, TimeoutError):
            healthy = False
        return DecimalJSONResponse(
            {"status": "HEALTHY" if healthy else "UNHEALTHY", "current_time": datetime.now(UTC)},
            status_code=200 if healthy else 503,
        )

    @application.post(
        "/v1/webhooks/transactions",
        status_code=202,
        response_model=Acknowledgment,
        tags=["Transactions"],
        responses={
            409: {"description": "ID reused with different data"},
            503: {"description": "Not acknowledged; sender should retry"},
        },
    )
    async def receive(transaction: TransactionInput, request: Request) -> Acknowledgment:
        try:
            # Bounds the storage path; no job processing takes place in this endpoint.
            async with asyncio.timeout(0.4):
                duplicate = await request.app.state.store.accept(transaction)
        except ConflictingTransaction as exc:
            raise HTTPException(
                409, "transaction_id already exists with different details"
            ) from exc
        except (DatabaseError, PoolTimeout, TimeoutError) as exc:
            logger.warning(
                "webhook_storage_unavailable",
                extra={
                    "transaction_id": transaction.transaction_id,
                },
            )
            raise unavailable() from exc
        return Acknowledgment(transaction_id=transaction.transaction_id, duplicate=duplicate)

    @application.get(
        "/v1/transactions/{transaction_id}",
        response_model=list[TransactionOutput],
        tags=["Transactions"],
        responses={404: {"description": "Unknown transaction"}},
    )
    async def get_transaction(transaction_id: str, request: Request) -> Response:
        try:
            async with asyncio.timeout(0.4):
                transaction = await request.app.state.store.get(transaction_id)
        except (DatabaseError, PoolTimeout, TimeoutError) as exc:
            raise unavailable() from exc
        if transaction is None:
            raise HTTPException(404, "Transaction not found")
        payload = TransactionOutput.model_validate(transaction)
        return DecimalJSONResponse([payload.model_dump(mode="python")])

    return application


app = create_app()
