import asyncio
import sys

from app.service import supervise


async def test_unexpected_child_exit_stops_its_sibling(tmp_path):
    marker = tmp_path / "terminated"
    sibling = (
        "import signal,time,pathlib,sys; "
        f"signal.signal(signal.SIGTERM, lambda *_: (pathlib.Path({str(marker)!r})"
        ".write_text('stopped'), sys.exit(0))); time.sleep(60)"
    )
    result = await asyncio.wait_for(
        supervise(
            [
                [sys.executable, "-c", sibling],
                [sys.executable, "-c", "import time; time.sleep(0.5)"],
            ]
        ),
        timeout=5,
    )
    assert result == 1
    assert marker.read_text() == "stopped"


async def test_unresponsive_child_is_killed_after_grace_period():
    result = await asyncio.wait_for(
        supervise(
            [
                [
                    sys.executable,
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(60)",
                ],
                [sys.executable, "-c", "import time; time.sleep(0.5)"],
            ],
            shutdown_timeout=0.1,
        ),
        timeout=5,
    )
    assert result == 1
