"""Measure real HTTP acknowledgment latency and wait for durable completion."""

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


async def benchmark(args: argparse.Namespace) -> dict:
    prefix = f"txn_bench_{uuid4().hex[:10]}"
    payloads = [
        {
            "transaction_id": f"{prefix}_{index}",
            "source_account": "acc_benchmark_user",
            "destination_account": "acc_benchmark_merchant",
            "amount": 1500,
            "currency": "INR",
        }
        for index in range(args.transactions)
    ]
    limit = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=10) as client:
        health = await client.get("/")
        health.raise_for_status()

        async def send(payload: dict) -> tuple[float, dict]:
            async with limit:
                started = perf_counter()
                response = await client.post("/v1/webhooks/transactions", json=payload)
                elapsed = (perf_counter() - started) * 1000
                if response.status_code != 202:
                    raise RuntimeError(f"Expected 202; got {response.status_code}: {response.text}")
                return elapsed, response.json()

        fresh = await asyncio.gather(*(send(payload) for payload in payloads))
        duplicates = await asyncio.gather(*(send(payloads[0]) for _ in range(args.duplicates)))
        if not all(result["duplicate"] for _, result in duplicates):
            raise RuntimeError("A repeated transaction was not recognized as a duplicate")
        latencies = [latency for latency, _ in fresh + duplicates]
        report = {
            "verified_at": datetime.now(UTC).isoformat(),
            "base_url": args.base_url,
            "unique_transactions": args.transactions,
            "duplicate_requests": args.duplicates,
            "concurrency": args.concurrency,
            "transport": "real HTTP, persistent connections",
            "acknowledgment_ms": {
                "p50": round(percentile(latencies, 0.5), 2),
                "p95": round(percentile(latencies, 0.95), 2),
                "p99": round(percentile(latencies, 0.99), 2),
                "maximum": round(max(latencies), 2),
            },
        }
        print(json.dumps(report, indent=2), flush=True)
        durations = []
        pending = {payload["transaction_id"] for payload in payloads}
        deadline = perf_counter() + args.timeout
        while pending and perf_counter() < deadline:
            for transaction_id in tuple(pending):
                response = await client.get(f"/v1/transactions/{transaction_id}")
                response.raise_for_status()
                transaction = response.json()[0]
                if transaction["status"] == "PROCESSED":
                    duration = (
                        datetime.fromisoformat(transaction["processed_at"])
                        - datetime.fromisoformat(transaction["created_at"])
                    ).total_seconds()
                    if duration < 30:
                        raise RuntimeError(
                            "Processing completed before the required 30-second delay"
                        )
                    durations.append(duration)
                    pending.remove(transaction_id)
            if pending:
                await asyncio.sleep(1)
        if pending:
            raise RuntimeError(f"Timed out waiting for {len(pending)} transactions")
        report["completed_transactions"] = len(durations)
        report["processing_seconds"] = {
            "minimum": round(min(durations), 3),
            "maximum": round(max(durations), 3),
        }
        report["acknowledgment_target_met"] = max(latencies) < 500
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        if not report["acknowledgment_target_met"]:
            raise RuntimeError("A webhook exceeded the 500 ms acknowledgment target")
        return report


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--transactions", type=positive, default=20)
    parser.add_argument("--duplicates", type=positive, default=40)
    parser.add_argument("--concurrency", type=positive, default=20)
    parser.add_argument("--timeout", type=positive, default=120)
    parser.add_argument("--output", type=Path)
    asyncio.run(benchmark(parser.parse_args()))
