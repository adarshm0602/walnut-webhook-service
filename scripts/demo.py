"""Exercise the public HTTP contract against a running API."""

import argparse
import asyncio
import json
from time import perf_counter
from uuid import uuid4

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    transaction_id = f"txn_demo_{uuid4().hex[:12]}"
    payload = {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    async with httpx.AsyncClient(base_url=args.base_url, timeout=10) as client:
        print("Health:", (await client.get("/")).json())
        started = perf_counter()
        response = await client.post("/v1/webhooks/transactions", json=payload)
        elapsed = (perf_counter() - started) * 1000
        response.raise_for_status()
        assert response.status_code == 202
        print(f"Accepted in {elapsed:.1f} ms:", response.json())
        duplicate = await client.post("/v1/webhooks/transactions", json=payload)
        assert duplicate.status_code == 202 and duplicate.json()["duplicate"]
        print("Duplicate safely acknowledged:", duplicate.json())
        for _ in range(120):
            current = await client.get(f"/v1/transactions/{transaction_id}")
            current.raise_for_status()
            transaction = current.json()[0]
            print(f"{perf_counter() - started:5.1f}s — {transaction['status']}")
            if transaction["status"] == "PROCESSED":
                print(json.dumps(transaction, indent=2))
                return
            await asyncio.sleep(1)
    raise SystemExit("Transaction did not complete within 120 seconds")


if __name__ == "__main__":
    asyncio.run(main())
