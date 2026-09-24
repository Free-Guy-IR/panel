import ast
import contextlib
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app import app_factory, lifecycle
from app.jobs import record_usages

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_STOP_TIMEOUT_S = 10.0
STOP_WINDOW_S = 9.5
UVICORN_SHUTDOWN_OVERHEAD_S = 0.2
PROCESS_EXIT_MARGIN_S = 0.3
TICK_DURATION_S = 4.0

SERVER_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import importlib
    import sys
    import time
    from datetime import UTC, datetime
    from pathlib import Path
    from unittest.mock import AsyncMock, Mock

    import uvicorn
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from fastapi import FastAPI

    from app import app_factory, lifecycle
    from app.fork import notification_spool
    from app.jobs import record_usages

    marks_path, port, tick_duration = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])


    def mark(name):
        with open(marks_path, "a") as handle:
            handle.write(f"{name} {time.monotonic()}\\n")


    scheduler = AsyncIOScheduler(timezone="UTC")
    importlib.import_module("app.scheduler").scheduler = scheduler
    leader = importlib.import_module("app.nats.leader")
    leader.set_on_leadership_lost = Mock()
    leader.start_job_leader = AsyncMock(return_value=True)
    leader.stop_job_leader = AsyncMock()
    lifecycle.startup_functions[:] = []
    lifecycle.shutdown_functions[:] = []
    lifecycle.final_shutdown_functions[:] = []
    notification_spool.SPOOL_PATH = Path(marks_path).with_name("notification_spool.json")


    async def slow_notification_flush():
        mark("flush-started")
        await asyncio.sleep(60)


    lifecycle.on_shutdown(slow_notification_flush)
    app_factory._register_scheduler_hooks()
    lifecycle.startup_functions[:] = []


    async def tick():
        mark("tick-read")
        await asyncio.sleep(tick_duration)
        mark("tick-persisted")


    async def usage_job():
        await record_usages._await_usage_job("record_user_usages", tick, 10, "JOB_RECORD_USER_USAGES_INTERVAL")


    app = FastAPI(lifespan=lifecycle.lifespan)


    @app.get("/ready")
    async def ready():
        return {}


    @app.get("/start-tick")
    async def start_tick():
        if not scheduler.running:
            scheduler.start()
        scheduler.add_job(usage_job, "date", run_date=datetime.now(UTC), id="usage-tick")
        return {}


    @app.get("/slow")
    async def slow():
        mark("request-started")
        await asyncio.sleep(60)
        return {}


    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=lifecycle.HTTP_DRAIN_TIMEOUT_S,
    )
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, timeout: float) -> int:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status


def _marks(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    return {name: float(stamp) for name, stamp in (line.split() for line in path.read_text().splitlines() if line)}


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_the_stop_window_is_split_so_http_drain_and_lifespan_fit_before_docker_kills_the_process():
    end_to_end = (
        UVICORN_SHUTDOWN_OVERHEAD_S
        + lifecycle.HTTP_DRAIN_TIMEOUT_S
        + lifecycle.SHUTDOWN_BUDGET_S
        + PROCESS_EXIT_MARGIN_S
    )
    assert end_to_end <= STOP_WINDOW_S <= DOCKER_STOP_TIMEOUT_S - 0.5

    drain_at_lifespan_start = (
        lifecycle.SHUTDOWN_BUDGET_S - lifecycle.SHUTDOWN_FINAL_RESERVE_S - app_factory.USAGE_DRAIN_TAIL_S
    )
    assert drain_at_lifespan_start >= record_usages.USAGE_PERSIST_MAX_VOLATILE_S + 1.0
    assert lifecycle.HTTP_DRAIN_TIMEOUT_S + drain_at_lifespan_start >= record_usages.USAGE_SHUTDOWN_GRACE_S


def test_main_passes_the_http_drain_timeout_to_uvicorn():
    tree = ast.parse((REPO_ROOT / "main.py").read_text())
    runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    ]
    assert len(runs) == 1
    keywords = {keyword.arg: keyword.value for keyword in runs[0].keywords}
    assert isinstance(keywords.get("timeout_graceful_shutdown"), ast.Name)
    assert keywords["timeout_graceful_shutdown"].id == "HTTP_DRAIN_TIMEOUT_S"


def test_split_role_entry_points_run_the_same_bounded_lifespan():
    for entry in ("node_worker.py", "scheduler_worker.py"):
        source = (REPO_ROOT / entry).read_text()
        assert "from app.lifecycle import lifespan" in source
        assert "async with lifespan(app)" in source


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signals")
def test_a_slow_request_and_a_slow_flush_still_exit_inside_the_stop_window_with_the_usage_tick_persisted(tmp_path):
    marks_path = tmp_path / "marks.txt"
    script = tmp_path / "server.py"
    script.write_text(SERVER_SCRIPT)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    process = subprocess.Popen(
        [sys.executable, str(script), str(marks_path), str(port), str(TICK_DURATION_S)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:

        def ready() -> bool:
            try:
                _get(f"{base}/ready", timeout=1)
                return True
            except urllib.error.URLError, ConnectionError, OSError:
                return False

        assert _wait_for(ready, 120), process.stdout.read().decode(errors="replace")[-3000:]

        _get(f"{base}/start-tick", timeout=5)
        assert _wait_for(lambda: "tick-read" in _marks(marks_path), 10)

        threading.Thread(target=lambda: _swallow(_get, f"{base}/slow", 30), daemon=True).start()
        assert _wait_for(lambda: "request-started" in _marks(marks_path), 10)

        sent = time.monotonic()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=DOCKER_STOP_TIMEOUT_S + 5)
        elapsed = time.monotonic() - sent
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    marks = _marks(marks_path)
    output = process.stdout.read().decode(errors="replace")
    assert elapsed <= STOP_WINDOW_S, f"the process took {elapsed:.2f}s to exit after SIGTERM\n{output[-3000:]}"
    assert "tick-persisted" in marks, f"the usage tick was lost at shutdown\n{output[-3000:]}"
    assert "flush-started" in marks
    assert marks["tick-persisted"] < marks["flush-started"]
    assert marks["tick-persisted"] - marks["tick-read"] >= TICK_DURATION_S


def _swallow(func, *args) -> None:
    with contextlib.suppress(Exception):
        func(*args)
