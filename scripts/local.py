"""Start/stop an isolated local PostgreSQL, API, and worker without Docker."""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
REGISTRY = LOCAL / "processes.json"
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import migrate  # noqa: E402


def pg_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        os.environ.get("POSTGRES_BIN_DIR", ""),
        "/opt/homebrew/opt/postgresql@17/bin",
        "/usr/local/opt/postgresql@17/bin",
    ]
    for directory in candidates:
        candidate = Path(directory) / name
        if directory and candidate.is_file():
            return str(candidate)
    raise SystemExit("PostgreSQL binaries missing. Install PostgreSQL 17 or use docker compose up.")


def registry() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
        return str(ROOT / ".venv") in command
    except (ProcessLookupError, subprocess.CalledProcessError):
        return False


def save(data: dict) -> None:
    REGISTRY.write_text(json.dumps(data, indent=2) + "\n")


def launch(name: str, module_args: list[str]) -> None:
    data = registry()
    if alive(data.get(name)):
        print(f"{name} already running (PID {data[name]})")
        return
    with (LOCAL / f"{name}.log").open("a") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", *module_args],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    data[name] = process.pid
    save(data)
    print(f"Started {name} (PID {process.pid})")


def stop_process(name: str) -> None:
    data = registry()
    pid = data.get(name)
    if alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
        print(f"Stopped {name}")
    data.pop(name, None)
    save(data)


def start_database() -> None:
    data = LOCAL / "postgres"
    socket_dir = LOCAL / "socket"
    socket_dir.mkdir(exist_ok=True)
    if not (data / "PG_VERSION").exists():
        subprocess.run(
            [
                pg_binary("initdb"),
                "-D",
                str(data),
                "-U",
                "walnut",
                "-A",
                "trust",
                "--encoding=UTF8",
                "--locale=C",
            ],
            check=True,
        )
    status = subprocess.run([pg_binary("pg_ctl"), "-D", str(data), "status"], capture_output=True)
    if status.returncode:
        # Only the project-owned cluster is started; no system service is changed.
        subprocess.run(
            [
                pg_binary("pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(LOCAL / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -p 55432 -k '{socket_dir}'",
                "-w",
                "start",
            ],
            check=True,
        )
    admin = "postgresql://walnut@127.0.0.1:55432/postgres"
    with psycopg.connect(admin, autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname = 'walnut'").fetchone():
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER walnut").format(sql.Identifier("walnut"))
            )


def wait_ready() -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise SystemExit(f"API failed to become ready. Check {LOCAL / 'api.log'}")


def start() -> None:
    settings = Settings()
    if settings.database_url == "postgresql://walnut@127.0.0.1:55432/walnut":
        start_database()
    migrate(settings.database_url)
    data = registry()
    if not alive(data.get("api")):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 8000)) == 0:
                raise SystemExit(
                    "Port 8000 is occupied by another service. Stop it before starting."
                )
    launch("api", ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"])
    launch("worker", ["app.worker"])
    wait_ready()
    time.sleep(0.3)
    if not alive(registry().get("worker")):
        raise SystemExit(f"Worker failed to start. Check {LOCAL / 'worker.log'}")
    print("\nReady: http://127.0.0.1:8000/docs")
    print(f"Logs and local database: {LOCAL}")


def main() -> None:
    os.chdir(ROOT)
    LOCAL.mkdir(exist_ok=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "restart-worker"])
    action = parser.parse_args().action
    if action == "start":
        start()
    elif action == "restart-worker":
        stop_process("worker")
        launch("worker", ["app.worker"])
    elif action == "stop":
        stop_process("worker")
        stop_process("api")
        data = LOCAL / "postgres"
        if (data / "PG_VERSION").exists():
            subprocess.run([pg_binary("pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"])
    else:
        for name in ("api", "worker"):
            print(f"{name}: {'running' if alive(registry().get(name)) else 'stopped'}")
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=1) as response:
                print(response.read().decode())
        except (urllib.error.URLError, TimeoutError):
            print("API health check unavailable")


if __name__ == "__main__":
    main()
