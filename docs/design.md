# Design decisions

## Acceptance is a durable promise

The API returns 202 only after both the transaction and its job have committed. Saving
a row and then enqueueing to another system would introduce a dual-write failure window.
For this workload, PostgreSQL holds both records in one atomic transaction.

The database primary keys enforce uniqueness even across simultaneous API instances.
An identical repeated payload receives 202; a changed payload under an existing ID
receives 409 and does not overwrite the accepted transaction.

## Processing does not hold a database connection

A worker slot claims an available job with `FOR UPDATE SKIP LOCKED`, records a lease token,
and commits. Its 30-second `asyncio.sleep` simulates external I/O outside the transaction.
Twenty slots can wait concurrently; the API and worker run as separate processes.
A shared claim lock serializes brief claims and empty-queue polling, avoiding twenty
redundant idle queries per polling interval. The 30-second work runs outside that lock.

The queue can build a backlog when arrival rate exceeds worker throughput. With 20 slots
and 30 seconds per transaction, nominal worker capacity is roughly 40 transactions per
minute. This is a sizing estimate, not a measured sustained-throughput guarantee. Extra
worker instances can claim from the same queue safely.

## Delivery and execution have different guarantees

Webhook duplicates create one job and one final transaction record. A crashed job can
execute again after its lease expires. This is at-least-once execution with idempotent
persistence, not exactly-once external effects. A real provider call would also need a
provider-supported idempotency key based on `transaction_id`.

Heartbeats extend leases for healthy workers. A different token fences out an old worker
after recovery. Job completion and the final transaction update commit together. If either
write fails, both roll back.

## Failure behavior is explicit

- Validation failure: 422, with no saved job and no raw financial payload echoed back.
- Storage failure or deadline: 503 and `Retry-After: 1`; the sender retries the same ID.
- Graceful worker shutdown: release unfinished jobs immediately.
- Hard crash: retry after lease expiry, then perform the 30-second simulation again.
- Transient processing error: persisted attempts and exponential backoff, capped at 60 seconds.
- Persistent errors: keep the job recoverable and log failures; do not silently discard it.

For a production system, alerts on queue age/retry count and a reviewed dead-letter policy
would prevent a permanent error from retrying unnoticed. These are documented extensions,
not claims about features implemented in this assessment.

## Decimal precision starts at JSON parsing

Using Decimal in a model is not sufficient if JSON was first decoded into a binary float.
A custom FastAPI request route parses numeric literals directly into Decimal. PostgreSQL
stores numeric(18,2), and the response serializer writes Decimal as a JSON number.
Regression tests cover large numeric literals, output precision, and invalid exponents.

## Operational scope

Root health checks the API's ability to access the transaction table. It does not certify
worker health. Structured worker logs, job attempts, and persisted final timestamps provide
diagnostic evidence. The benchmark measures real HTTP latency from its caller's location;
localhost results do not establish cloud latency.

The public assessment API accepts the supplied unauthenticated test contract. Actual
payment ingestion would add provider signature verification, rate limiting, credential
management, monitoring, and retention requirements. No real account balances are modified.

## Free cloud demo

The Render demo uses one free Docker web service with separate supervised API/worker
processes and free PostgreSQL on the private network in Singapore. This reduces hosting
cost to zero at the expense of process isolation and always-on availability. The supervisor
terminates both processes if either exits unexpectedly. Migrations run before child startup.

After 15 minutes without incoming traffic, the web service sleeps and background work
pauses. A later request wakes it; pending jobs are persisted and can be reclaimed. Cold
starts exceed the 500 ms target. Free PostgreSQL expires on 22 November 2026. Warm latency
and burst capacity are measured separately in the verification report. Always-on hosting
would be needed for a dependable first-request latency target.
