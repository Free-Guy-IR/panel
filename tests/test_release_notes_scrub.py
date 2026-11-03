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
    body = text.split("> CHANGELOG.raw\n", 1)[1].split("\n            - name: ", 1)[0]
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


FAKE_GIT = """#!/usr/bin/env bash
printf 'a1\\tfix: $(touch PWNED) quoted `touch PWNED2`\\n'
printf 'b2\\tchore(deps): bump something\\n'
printf 'c3\\tfeat(api)!: breaking thing\\n'
printf 'd4\\tMerge branch main\\n'
printf 'e5\\tfix: bot change\\n'
printf 'f6\\tchore(release): v9.9.9\\n'
printf 'g7\\tdocs: same author again\\n'
"""

FAKE_GH = """#!/usr/bin/env bash
case "$2" in
  */a1) echo alice ;;
  */b2) echo deps-person ;;
  */c3) echo Bob ;;
  */e5) echo 'dependabot[bot]' ;;
  */f6) echo releaser ;;
  */g7) echo alice ;;
esac
"""


def _contributors_from_workflow() -> str:
    text = WORKFLOW.read_text()
    step = text.split("            - name: Add GitHub contributors\n", 1)[1].split("\n            - name: ", 1)[0]
    body = step.split("              run: |\n", 1)[1]
    lines = [ln[18:] if ln.startswith(" " * 18) else ln.strip() for ln in body.splitlines()]
    return "set -u\n" + "\n".join(lines) + "\n"


@pytest.fixture
def contributors_run(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is unavailable")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("git", FAKE_GIT), ("gh", FAKE_GH)):
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o755)
    (tmp_path / "CHANGELOG.md").write_text("## v9.9.9\n")
    (tmp_path / "contributors.sh").write_text(_contributors_from_workflow())
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GITHUB_REPOSITORY": "owner/repo",
        "PREVIOUS_TAG": "v9.9.8",
        "CURRENT_TAG": "v9.9.9",
    }
    result = subprocess.run(
        ["bash", "contributors.sh"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path


def test_contributors_step_never_runs_commit_subjects(contributors_run):
    assert not (contributors_run / "PWNED").exists()
    assert not (contributors_run / "PWNED2").exists()


def test_contributors_step_lists_human_logins_once(contributors_run):
    notes = (contributors_run / "CHANGELOG.md").read_text()
    assert notes == "## v9.9.9\n\n### Contributors\n\n- @alice\n- @Bob\n"


def test_contributors_step_appends_no_address(contributors_run):
    assert ADDRESS.search((contributors_run / "CHANGELOG.md").read_text()) is None
