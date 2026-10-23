# Verification evidence

Verified locally on 23 October 2026 using Python 3.12.12 and PostgreSQL 17.

## Automated integration tests

`uv run python -m pytest -v -s` — **31 passed in 33.77 seconds**.

The transaction suite uses real PostgreSQL with an isolated schema per test. Two additional
process-supervisor tests verify sibling termination and forced shutdown after the grace period. It includes the real
30-second simulation under 20 concurrent transactions, simultaneous duplicates, exact
numeric JSON amounts, invalid numbers, database lock timeouts, rollback on both acceptance
and completion failures, lease renewal, stale-worker fencing, and retry recovery.

In the 20-transaction integration scenario, maximum acknowledgment time was 45.1 ms and
completion was 30.00–30.01 seconds. This test uses an in-process ASGI HTTP transport; the
separate benchmark below uses TCP requests to the running service.

## Real HTTP benchmark

Reproduce with:

```sh
uv run python scripts/benchmark.py --output .local/benchmark.json
```

The checked-in [JSON report](benchmark-local.json) records 20 new transactions, 40 duplicate
deliveries, and request concurrency capped at 20:

| Measurement | Observed locally |
| --- | ---: |
| Acknowledgment p50 | 22.03 ms |
| Acknowledgment p95 | 81.17 ms |
| Acknowledgment p99 / maximum | 89.03 ms |
| Completed transactions | 20 / 20 |
| Processing duration | 30.122–30.133 seconds |

These figures are one local run, not a guarantee for arbitrary load or cloud regions.
Cloud results are measured separately below.

## Process-level recovery

The initial implementation was also exercised outside the test harness:

1. Send and persist a new webhook; wait for its job to be claimed.
2. Send SIGKILL to the managed worker process during the 30-second simulation.
3. Resend the identical webhook; receive 202 without creating another job.
4. Start a replacement worker. The 60-second lease expires and it retries the job.
5. Observe one final PROCESSED transaction with job attempts = 2.
6. Restart the API, worker, and PostgreSQL; verify the stored result is unchanged.

Measured recovery was **89.66 seconds after replacement startup**, including waiting for
the real lease to expire and repeating the 30-second work. Recovery does not promise a
30-second completion when a worker crashes.

## Public cloud verification

[Public API](https://walnut-transactions-free.onrender.com/) ·
[Interactive documentation](https://walnut-transactions-free.onrender.com/docs) ·
[Cloud JSON report](benchmark-cloud.json)

Verified on 23 October 2026 at commit `4412467`, with a free Render Docker web service
and private-network free PostgreSQL in Singapore. The client was the developer's macOS
machine (configured timezone Asia/Kolkata). HTTP measurements include the network path;
the health request wakes the service before timing POST requests.

```sh
uv run python scripts/benchmark.py \
  --base-url https://walnut-transactions-free.onrender.com \
  --concurrency 4 --output .local/benchmark-cloud.json
```

| Measurement | Warm free cloud service |
| --- | ---: |
| Unique transactions / duplicate deliveries | 20 / 40 |
| Maximum in-flight HTTP requests | 4 |
| Acknowledgment p50 | 146.55 ms |
| Acknowledgment p95 | 276.43 ms |
| Acknowledgment p99 / maximum | 445.91 ms |
| Completed transactions | 20 / 20 |
| Processing duration | 30.679–30.967 seconds |

All 60 requests returned 202; duplicates were recognized, and all 20 final records
were retrieved. A separate **concurrency-20 stress attempt returned retryable 503s** on
the free tier. The measured light-load pass does not establish the same latency at higher
load. The worker was improved to share idle queue polling across its 20 slots, reducing
empty queries from 20 per interval to one.

The browser flow submitted `txn_cloud_browser_001` via public Swagger: 202 with
`duplicate: false`, then a persisted `PROCESSED` result after 30.068 seconds. That record
retained its original final timestamps after the next service redeployment. Public checks
also verify identical duplicates (202), conflicting payloads (409), invalid input (422),
and unknown IDs (404).

### Hosting limits

The free service sleeps after 15 idle minutes; waking can take 50 seconds or more and
**does not meet the 500 ms target**. Worker processing pauses while asleep and resumes
from durable jobs on wake. The free database expires **22 November 2026**. Export or
upgrade before expiry to retain data. Workspace free instance hours are shared. There is
no claim of an always-on SLA; that would need different hosting.

## CI and container verification

[Successful submission CI](https://github.com/adarshm0602/walnut-webhook-service/actions/runs/39937399339)
passed all 31 tests, lint/format checks, and a Docker Compose build with a real HTTP
acceptance/duplicate/30-second completion demo. The free supervised deployment was
additionally verified through the public API and browser as described above.
