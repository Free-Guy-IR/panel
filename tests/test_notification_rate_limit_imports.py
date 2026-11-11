import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "statement",
    [
        "import app.fork.notification_rate_limit",
        "from app.fork import notification_rate_limit, notification_spool",
    ],
)
def test_the_notification_rate_limit_module_imports_first_in_a_fresh_interpreter(statement):
    result = subprocess.run(
        [sys.executable, "-c", statement],
        cwd=ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-3000:]
