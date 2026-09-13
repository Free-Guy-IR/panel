import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

RAW = """## v9.9.9

### 🚀 Enhancements

- **mail:** support [our team](mailto:team@company.io) as sender
- **panel:** plain link <https://example.com/x> stays

#### ❤️ Contributors

- Someone <someone@example.com>
- Another <a.b-c_d@sub.domain.co.uk>

### 🩹 Fixes

- **bulk:** this entry follows the removed section and must survive
- credit: reporter@example.net found it

## v9.9.8

### 📖 Documentation

- a section in a later version block
"""


def _sanitizer_from_workflow() -> str:
    text = WORKFLOW.read_text()
    body = text.split("> CHANGELOG.raw\n", 1)[1].split("            - name: Update release notes", 1)[0]
    lines = [ln[18:] if ln.startswith(" " * 18) else ln.strip() for ln in body.splitlines() if ln.strip()]
    return "set -u\n" + "\n".join(lines) + "\n"


@pytest.fixture
def scrubbed(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is unavailable")
    (tmp_path / "CHANGELOG.raw").write_text(RAW)
    (tmp_path / "scrub.sh").write_text(_sanitizer_from_workflow())
    result = subprocess.run(["bash", "scrub.sh"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return (tmp_path / "CHANGELOG.md").read_text()


def test_no_address_of_any_form_survives(scrubbed):
    assert ADDRESS.search(scrubbed) is None


def test_the_contributors_heading_is_removed(scrubbed):
    assert not re.search(r"(?m)^#+\s*.*Contributors", scrubbed)


def test_entries_that_merely_mention_an_address_are_kept(scrubbed):
    assert "support [our team]" in scrubbed
    assert "credit:" in scrubbed


def test_a_plain_link_is_left_alone(scrubbed):
    assert "https://example.com/x" in scrubbed


def test_sections_following_the_removed_one_survive(scrubbed):
    assert "must survive" in scrubbed
    assert "a section in a later version block" in scrubbed
    assert "## v9.9.8" in scrubbed


def test_the_step_refuses_to_publish_when_an_address_gets_through(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is unavailable")
    script = _sanitizer_from_workflow().replace(
        "| cat -s > CHANGELOG.md", "| cat -s > /dev/null; cp CHANGELOG.raw CHANGELOG.md"
    )
    (tmp_path / "CHANGELOG.raw").write_text("- reach me at someone@example.com\n")
    (tmp_path / "scrub.sh").write_text(script)
    result = subprocess.run(["bash", "scrub.sh"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "refusing to publish" in result.stdout
