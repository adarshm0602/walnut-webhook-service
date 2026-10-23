# Verification evidence

Verified locally on 23 October 2026 using Python 3.12.12 and PostgreSQL 17.

## Automated integration tests

`uv run python -m pytest -v -s` — **29 passed in 32.69 seconds**.

The suite uses real PostgreSQL with an isolated schema per test. It includes the real
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
The public deployment needs its own run from an identified client location.

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

## CI and container verification

[Initial successful CI run](https://github.com/adarshm0602/walnut-webhook-service/actions/runs/39931553843)
validated the original suite and built the Docker Compose stack before running a real HTTP
demo to completion. The submission improvements are subject to the same CI workflow;
check the latest branch run before submission.
