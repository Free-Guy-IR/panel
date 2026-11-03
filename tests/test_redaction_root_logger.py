import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.fork.operation.node_messages import scrub_credentials

SECRET = "Zq7RootPw42"
BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
REPOSITORY = Path(__file__).resolve().parent.parent
MEGABYTE = 1 << 20


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


def test_an_unconfigured_child_logger_record_reaches_the_root_without_the_proxy_password_or_bot_token():
    finished = _run(
        f"""
        import logging

        import app.utils.logger

        logging.getLogger("aiohttp.client").warning("proxy http://user:{SECRET}@10.0.0.1:3128 refused the tunnel")
        logging.getLogger("aiogram.dispatcher").error("polling via %s failed", "socks5://bot:{SECRET}@10.0.0.2:1080")
        logging.getLogger("aiogram.session").error("POST https://api.telegram.org/bot{BOT_TOKEN}/getUpdates timed out")
        try:
            raise ConnectionError("Cannot connect through http://user:{SECRET}@10.0.0.1:3128")
        except ConnectionError:
            logging.getLogger("asyncio").exception("Task exception was never retrieved")
        logging.warning("root warning for http://admin:{SECRET}@10.0.0.3:8080")
        logging.basicConfig()
        logging.getLogger("aiohttp.client").warning("after basicConfig http://user:{SECRET}@10.0.0.1:3128")
        logging.getLogger("aiogram.session").warning("after basicConfig /bot{BOT_TOKEN}/sendMessage")
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert SECRET not in printed
    assert BOT_TOKEN.split(":")[1] not in printed
    for kept in (
        "refused the tunnel",
        "polling via",
        "timed out",
        "Cannot connect through",
        "Task exception was never retrieved",
        "root warning for",
        "after basicConfig http",
        "after basicConfig /bot",
    ):
        assert kept in printed


def test_an_app_record_is_printed_once_and_a_library_with_its_own_handler_stays_silent():
    finished = _run(
        f"""
        import logging

        from app.utils.logger import get_logger

        get_logger("root-logging-probe").warning("probe line once http://user:{SECRET}@10.0.0.1:3128")
        quiet = logging.getLogger("quiet.library")
        quiet.addHandler(logging.NullHandler())
        quiet.warning("library line that its own handler swallows")
        logging.getLogger("aiohttp.client").info("below the last-resort level")
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert printed.count("probe line once") == 1
    assert SECRET not in printed
    assert "library line that its own handler swallows" not in printed
    assert "below the last-resort level" not in printed


def test_uvicorn_handlers_that_bypass_the_root_print_without_the_proxy_password():
    finished = _run(
        f"""
        import logging
        import logging.config

        from app.utils.logger import LOGGING_CONFIG

        logging.config.dictConfig(LOGGING_CONFIG)
        logging.getLogger("uvicorn.error").error("startup via http://user:{SECRET}@10.0.0.1:3128 failed")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/x?p=http://u:{SECRET}@h", "1.1", 200
        )
        """
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout + finished.stderr
    assert SECRET not in printed
    assert "startup via" in printed


def test_the_root_filter_scrubs_a_megabyte_of_adversarial_text_in_linear_time():
    finished = _run(
        """
        import json, logging, sys, time

        from app.fork.log_scrub import CredentialScrubFilter

        text = sys.stdin.read()
        record = logging.LogRecord("x", logging.WARNING, "probe.py", 1, "%s", (text,), None)
        started = time.perf_counter()
        CredentialScrubFilter().filter(record)
        elapsed = time.perf_counter() - started
        print(json.dumps({"elapsed": elapsed, "leaked": "hunter2secret" in record.getMessage()}))
        """,
        stdin=("/BOT1:" + "a:b@" * (MEGABYTE // 10)) + " socks5://u:hunter2secret@h " + "1234567%3A" * (MEGABYTE // 20),
    )
    assert finished.returncode == 0, finished.stderr
    result = json.loads(finished.stdout.strip().splitlines()[-1])
    assert result["elapsed"] < 1.0, result
    assert not result["leaked"], result


@pytest.mark.parametrize(
    "text",
    [
        f"https://api.telegram.org/BOT{BOT_TOKEN}/sendMessage",
        f"https://api.telegram.org/Bot{BOT_TOKEN}/sendMessage",
        f"token={BOT_TOKEN}.",
        f"bot{BOT_TOKEN} rejected",
        f"https://api.telegram.org/bot{BOT_TOKEN.replace(':', '%3A')}/x",
        f"https://api.telegram.org/bot{BOT_TOKEN.replace(':', '%3a')}/x",
    ],
)
def test_every_bot_token_form_is_masked(text):
    scrubbed = scrub_credentials(text)
    assert BOT_TOKEN.split(":")[1] not in scrubbed


@pytest.mark.parametrize(
    "text, kept",
    [
        ("http%3A%2F%2Fuser%3APr0xyS3cret%40proxy", "proxy"),
        ("proxy=socks5%3A%2F%2Fu%3APr0xyS3cret%40h%3A1080 failed", "failed"),
        ("password=" + "Pr0xyS3cret failed", "failed"),
        ("PASSWD=" + "Pr0xyS3cret&x=1", "x=1"),
    ],
)
def test_percent_encoded_urls_and_password_pairs_are_masked(text, kept):
    scrubbed = scrub_credentials(text)
    assert "Pr0xyS3cret" not in scrubbed
    assert kept in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "Error: connection timed out at 12:30:45",
        "node 12 at [2001:db8::1]:62050 refused",
        "user@host->http://foo",
        "ids 1234567:short",
    ],
)
def test_harmless_text_is_left_alone(text):
    assert scrub_credentials(text) == text
