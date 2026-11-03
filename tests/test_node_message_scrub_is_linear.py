import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.fork.operation.node_messages import scrub_and_truncate, scrub_credentials

MEGABYTE = 1 << 20
SECRET = "hunter2secret"

ADVERSARIAL_SHAPES = {
    "schemes_without_at": "a://" * (MEGABYTE // 4),
    "schemes_then_credentials": "a://" * (MEGABYTE // 4) + "user:hunter2secret@proxy.example.com",
    "dotted_scheme_run": "a." * (MEGABYTE // 2) + "://user:hunter2secret@proxy.example.com",
    "long_colon_run": "a:b" * (MEGABYTE // 3),
    "long_colon_run_then_at": "a:b" * (MEGABYTE // 3) + "@proxy.example.com",
    "many_at_signs": "@" * MEGABYTE,
    "lead_colon_runs": "=a:" * (MEGABYTE // 3),
    "lead_colon_runs_after_at": "@" + "=a:" * (MEGABYTE // 3),
    "lead_slash_runs_then_credentials": "=a/" * (MEGABYTE // 3) + ",user:hunter2secret@proxy.example.com",
    "empty_leads_then_credentials": "," * MEGABYTE + "user:hunter2secret@proxy.example.com",
    "password_of_at_signs": "socks5://u:hunter2secret" + "@" * MEGABYTE + "proxy.example.com",
    "many_small_credential_tokens": "u:hunter2secret@h " * (MEGABYTE // 18),
    "many_small_url_tokens": "a://u:hunter2secret@h " * (MEGABYTE // 22),
}

_CHILD = """
import json, sys, time
from app.fork.operation.node_messages import scrub_and_truncate, scrub_credentials
text = sys.stdin.read()
started = time.perf_counter()
scrubbed = scrub_credentials(text)
truncated = scrub_and_truncate(text, 4096, "...")
elapsed = time.perf_counter() - started
print(json.dumps({"elapsed": elapsed, "leaked": "hunter2secret" in scrubbed or "hunter2secret" in truncated, "size": len(text)}))
"""


@pytest.mark.parametrize("shape", sorted(ADVERSARIAL_SHAPES))
def test_scrubbing_a_megabyte_of_adversarial_text_is_fast_and_still_scrubs(shape):
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD],
            input=ADVERSARIAL_SHAPES[shape],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            cwd=Path(__file__).resolve().parents[1],
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"scrubbing {shape} did not finish within 20 s")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["size"] >= MEGABYTE - 32
    assert result["elapsed"] < 0.5, result
    assert not result["leaked"], result


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a://" * 8 + "user:hunter2secret@proxy.example.com", "a://***@proxy.example.com"),
        ("socks5://u:p@ss@proxy.example.com:1080 refused", "socks5://***@proxy.example.com:1080 refused"),
        ("proxy=user:x@socks5://u:p@h", "proxy=***@h"),
        ("(socks5://u:p@h)", "(socks5://***@h)"),
        ("u:hunter2secret@h u:hunter2secret@h", "***@h ***@h"),
        ("via=socks5://u:p@h and user:pw@other", "via=socks5://***@h and ***@other"),
        ("a:b" * 50, "a:b" * 50),
        ("=a:" * 50, "=a:" * 50),
        ("@" + "=a:" * 50, "@" + "=a:" * 50),
        ("user@host->http://foo", "user@host->http://foo"),
        ("user:pass@host/x://y", "***@host/x://y"),
    ],
)
def test_linear_scrub_matches_the_expected_output(text, expected):
    assert scrub_credentials(text) == expected


def test_scrub_runs_before_truncation_on_huge_input():
    text = "x" * MEGABYTE + " socks5://user:hunter2secret@proxy.example.com"
    assert SECRET not in scrub_and_truncate(text[-200:], 60, "...")
    assert SECRET not in scrub_and_truncate(text, MEGABYTE + 30, "...")
