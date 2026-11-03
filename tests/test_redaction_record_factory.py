import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.fork.operation.node_messages import scrub_credentials

SECRET = "S3cr3tX"
BOT_SECRET = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
REPOSITORY = Path(__file__).resolve().parent.parent
KILOBYTE = 1 << 10


def _run(script: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=REPOSITORY,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_handler_a_library_attaches_after_import_prints_no_secret_and_no_traceback_secret():
    finished = _run(
        f"""
        import logging
        import sys

        import app.utils.logger

        late = logging.getLogger("late")
        late.addHandler(logging.StreamHandler(sys.stdout))
        late.propagate = False
        late.error("x %s", "http://u:{SECRET}@h:1")
        record = late.makeRecord("late", logging.ERROR, "f.py", 1, "y %s", ("http://u:{SECRET}@h:1",), None)
        print("message", record.getMessage())
        try:
            raise ConnectionError("cannot reach http://u:{SECRET}@h:1")
        except ConnectionError:
            late.exception("late traceback")
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert SECRET not in printed
    assert printed.count("x http://") == 1
    assert "message y http://***@h:1" in printed
    assert "late traceback" in printed
    assert "ConnectionError: cannot reach" in printed


def test_basic_config_force_and_dict_config_after_import_still_print_no_secret():
    finished = _run(
        f"""
        import logging
        import logging.config
        import sys

        import app.utils.logger

        logging.basicConfig(force=True, stream=sys.stdout)
        logging.getLogger("forced").error("forced %s", "socks5://u:{SECRET}@h:1080")
        try:
            raise OSError("proxy http://u:{SECRET}@h:1 refused")
        except OSError:
            logging.getLogger("forced").exception("forced traceback")
        logging.config.dictConfig({{
            "version": 1,
            "handlers": {{"out": {{"class": "logging.StreamHandler", "stream": "ext://sys.stdout"}}}},
            "root": {{"handlers": ["out"], "level": "INFO"}},
        }})
        logging.getLogger("configured").warning("configured http://u:{SECRET}@h:1")
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert SECRET not in printed
    for kept in ("forced socks5://", "forced traceback", "OSError: proxy", "configured http://"):
        assert kept in printed


def test_a_custom_formatter_that_formats_the_exception_itself_prints_no_secret():
    finished = _run(
        f"""
        import logging
        import sys

        import app.utils.logger


        class RawFormatter(logging.Formatter):
            def format(self, record):
                return record.getMessage() + " | " + self.formatException(record.exc_info)


        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(RawFormatter())
        raw = logging.getLogger("raw")
        raw.addHandler(handler)
        raw.propagate = False
        try:
            raise ValueError("bad http://u:{SECRET}@h:1")
        except ValueError:
            raw.exception("raw")
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert SECRET not in printed
    assert "ValueError: bad http://" in printed


def test_a_factory_set_before_import_is_chained_and_the_scrub_runs_once():
    finished = _run(
        f"""
        import logging
        import sys

        import app.fork.operation.node_messages as node_messages

        previous = logging.getLogRecordFactory()


        def tagging_factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            record.tagged = True
            return record


        logging.setLogRecordFactory(tagging_factory)

        import app.utils.logger
        import app.fork.log_scrub as log_scrub

        calls = []
        original = log_scrub.scrub_credentials


        def counting(text):
            calls.append(1)
            return original(text)


        log_scrub.scrub_credentials = counting
        node_messages.scrub_credentials = counting
        app.utils.logger.get_logger("reinstall-probe")
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(tagged)s %(message)s"))
        once = logging.getLogger("once")
        once.addHandler(handler)
        once.propagate = False
        calls.clear()
        once.error("once %s", "http://u:{SECRET}@h:1")
        print("calls", len(calls))
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert SECRET not in printed
    assert "True once http://***@h:1" in printed
    assert printed.count("once http://") == 1
    calls = int(printed.split("calls ")[1].split()[0])
    assert calls <= 2, printed


def test_queue_handler_and_listener_print_no_secret():
    finished = _run(
        f"""
        import logging
        import logging.handlers
        import queue
        import sys

        import app.utils.logger

        records = queue.Queue()
        listener = logging.handlers.QueueListener(records, logging.StreamHandler(sys.stdout))
        queued = logging.getLogger("queued")
        queued.addHandler(logging.handlers.QueueHandler(records))
        queued.propagate = False
        listener.start()
        queued.error("queued %s", "http://u:{SECRET}@h:1")
        try:
            raise RuntimeError("queued http://u:{SECRET}@h:1")
        except RuntimeError:
            queued.exception("queued traceback")
        listener.stop()
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert SECRET not in printed
    assert "queued traceback" in printed


@pytest.mark.parametrize(
    "text, kept",
    [
        (f"password: {SECRET}", "password: "),
        (f"Password:{SECRET} rejected", "rejected"),
        (f'{{"password": "{SECRET}", "user": "a"}}', '"user": "a"'),
        (f'{{"proxy_password":"{SECRET}"}}', '"proxy_password":"'),
        (f"password='{SECRET}' failed", "failed"),
        (f'password="{SECRET}" failed', "failed"),
        (f"pwd: {SECRET}", "pwd: "),
        (f"pa\u017f\u017fword: {SECRET}", "word: "),
        (f'PA\u017f\u017fWORD="{SECRET}"', 'WORD="'),
    ],
)
def test_password_colon_and_json_values_are_masked(text, kept):
    scrubbed = scrub_credentials(text)
    assert SECRET not in scrubbed
    assert kept in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "password" + ": required",
        "password: too short",
        "field password: missing",
        "the password: must be at least 8 characters",
        'error "password" is required',
        "password:",
    ],
)
def test_password_wording_without_a_value_is_left_alone(text):
    assert scrub_credentials(text) == text


@pytest.mark.parametrize(
    "text",
    [
        f"TOKEN_123456789:{BOT_SECRET}",
        f"bot_token=_8123456789:{BOT_SECRET}",
    ],
)
def test_a_bot_token_after_an_underscore_is_masked(text):
    assert BOT_SECRET not in scrub_credentials(text)


@pytest.mark.parametrize(
    "text",
    [
        "build_2026:release",
        "ids 1234567:short",
        "line 12:30 ok",
    ],
)
def test_underscore_and_short_id_text_is_left_alone(text):
    assert scrub_credentials(text) == text


def test_the_scrub_stays_linear_between_half_a_megabyte_and_a_megabyte():
    finished = _run(
        """
        import json, sys, time

        from app.fork.operation.node_messages import scrub_credentials

        unit = sys.stdin.read()

        def best(text):
            timings = []
            for _ in range(3):
                started = time.perf_counter()
                scrub_credentials(text)
                timings.append(time.perf_counter() - started)
            return min(timings)

        half = best(unit * 512)
        full = best(unit * 1024)
        print(json.dumps({"half": half, "full": full}))
        """,
        stdin=("a:b@password" + ': _1234567%3A"pwd":"x ') * (KILOBYTE // 34),
    )
    assert finished.returncode == 0, finished.stderr
    result = json.loads(finished.stdout.strip().splitlines()[-1])
    assert result["full"] < 2.0, result
    assert result["full"] < 3 * max(result["half"], 1e-3), result
