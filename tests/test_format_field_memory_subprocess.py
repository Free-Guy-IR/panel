import json
import subprocess
import sys
from pathlib import Path

import pytest

PEAK_LIMIT = 16 * 1024 * 1024
HUGE_WIDTH = 400_000_000

PROBE = """
import json
import sys
import tracemalloc
from types import SimpleNamespace

from app.operation.subscription import SubscriptionOperation
from app.subscription.share import apply_custom_format_variables

case, template = sys.argv[1], sys.argv[2]
variables = {"USERNAME": "alice", "DAYS_LEFT": 12, "USAGE": 15.25}
settings = SimpleNamespace(profile_title=template, announce=template, announce_url=template)
user = SimpleNamespace(admin=None)

tracemalloc.start()
if case == "profile_title":
    result = SubscriptionOperation._format_profile_title(user, dict(variables), settings)
elif case == "announce":
    result = SubscriptionOperation._format_announce(settings, dict(variables))
elif case == "announce_url":
    result = SubscriptionOperation._format_announce_url(settings, dict(variables))
else:
    result = apply_custom_format_variables(dict(variables), [{"key": "PAD", "value": template}])["PAD"]
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(json.dumps({"peak": peak, "length": len(result)}))
"""

CASES = ["profile_title", "announce", "announce_url", "custom_variable"]
TEMPLATES = [
    f"{{USERNAME:>{HUGE_WIDTH}}}",
    f"{{DAYS_LEFT:0{HUGE_WIDTH}}}",
    f"{{USAGE:.{HUGE_WIDTH}f}}",
    f"{{USERNAME:_<{HUGE_WIDTH}}}",
]


def _probe(case: str, template: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", PROBE, case, template],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("case", CASES)
def test_a_stored_huge_width_or_precision_never_allocates_the_padded_string(case, template):
    measured = _probe(case, template)
    assert measured["peak"] < PEAK_LIMIT, measured
    assert measured["length"] <= len(template) + 4096, measured
