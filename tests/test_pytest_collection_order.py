import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
API_AUTOUSE_MOCKS = {"mock_db_session", "mock_lock", "mock_settings"}
FIXTURES_USED = re.compile(r"^\s*(\S+::\S+) \(fixtures used: ([^)]*)\)")


@pytest.fixture(scope="module")
def interleaved_run(tmp_path_factory) -> tuple[subprocess.CompletedProcess, dict[str, set[str]]]:
    tmp_path = tmp_path_factory.mktemp("collection_order")
    env = dict(os.environ)
    env["TEST_FROM"] = "local"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'order.db'}"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--setup-show",
            "tests/api/test_settings.py::test_general_settings_custom_variables_round_trip",
            "tests/test_shutdown_order.py",
            "tests/api/test_sub_revoke_same_second.py",
        ],
        check=False,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    used = {}
    for line in result.stdout.splitlines():
        match = FIXTURES_USED.match(line)
        if match:
            used[match.group(1)] = {name.strip() for name in match.group(2).split(",")}
    return result, used


def test_api_conftest_fixtures_survive_a_top_level_file_between_api_files(interleaved_run):
    result, used = interleaved_run
    output = result.stdout + result.stderr

    assert "fixture 'access_token' not found" not in output
    assert result.returncode == 0, output[-4000:]
    assert re.search(r"\b3 passed\b", result.stdout), output[-4000:]

    revoke = "tests/api/test_sub_revoke_same_second.py::test_a_token_minted_before_the_revoke_is_rejected"
    assert revoke in used, output[-4000:]
    assert "access_token" in used[revoke]
    assert API_AUTOUSE_MOCKS <= used[revoke]


def test_api_autouse_mocks_never_reach_a_top_level_test(interleaved_run):
    result, used = interleaved_run
    output = result.stdout + result.stderr

    top_level = [nodeid for nodeid in used if nodeid.startswith("tests/test_shutdown_order.py::")]
    assert top_level, output[-4000:]
    for nodeid in top_level:
        assert not (API_AUTOUSE_MOCKS & used[nodeid]), (nodeid, used[nodeid])

    api = [nodeid for nodeid in used if nodeid.startswith("tests/api/")]
    assert len(api) == 2, output[-4000:]
    for nodeid in api:
        assert API_AUTOUSE_MOCKS <= used[nodeid], (nodeid, used[nodeid])
