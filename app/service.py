"""Supervise separate API and worker processes in a single free Render container."""

import asyncio
import os
import signal
import sys


async def supervise(commands: list[list[str]], shutdown_timeout: float = 15) -> int:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    children = []
    tasks = []
    try:
        for command in commands:
            children.append(await asyncio.create_subprocess_exec(*command))
        tasks = [asyncio.create_task(child.wait()) for child in children]
        stop_task = asyncio.create_task(stopped.wait())
        tasks.append(stop_task)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Any unexpected child exit, including exit 0, restarts the whole service.
        return 0 if stop_task in done else 1
    finally:
        for child in children:
            if child.returncode is None:
                child.terminate()
        if children:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(child.wait() for child in children)), shutdown_timeout
                )
            except TimeoutError:
                for child in children:
                    if child.returncode is None:
                        child.kill()
                await asyncio.gather(*(child.wait() for child in children))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


async def run() -> int:
    migration = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.migrate")
    if await migration.wait() != 0:
        return 1
    return await supervise(
        [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                os.environ.get("PORT", "8000"),
            ],
            [sys.executable, "-m", "app.worker"],
        ]
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
