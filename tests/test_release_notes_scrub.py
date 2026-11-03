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


FAKE_GIT = r"""#!/usr/bin/env bash
grep_for=""
format=""
for arg in "$@"; do
  case "$arg" in
    --grep=*) grep_for="${arg#--grep=}" ;;
    --format=*) format="${arg#--format=}" ;;
  esac
done
emit() {
  if [[ -n "$grep_for" && "$3" != *"$grep_for"* ]]; then
    return
  fi
  if [[ "$format" == '%H' ]]; then
    printf '%s\n' "$1"
  else
    printf '%s\t%s\n' "$1" "$2"
  fi
}
emit a1 'fix: $(touch PWNED) quoted `touch PWNED2`' ''
emit b2 'chore(deps): bump something' ''
emit c3 'feat(api)!: breaking thing' ''
emit d4 'Merge branch main' ''
emit e5 'fix: bot change' ''
emit f6 'chore(release): v9.9.9' ''
emit g7 'docs: same author again' ''
emit h8 'fix(node): ported upstream fix' $'fix(node): ported upstream fix\n\n(cherry picked from commit 0123456789abcdef0123456789abcdef01234567)'
emit i9 'feat: change merged from upstream' 'feat: change merged from upstream'
emit j0 'fix: says cherry picked from commit in its subject' 'fix: says cherry picked from commit in its subject'
"""

FAKE_GH = r"""#!/usr/bin/env bash
case "$2" in
  */commits/a1) echo alice ;;
  */commits/b2) echo deps-person ;;
  */commits/c3) echo Bob ;;
  */commits/e5) echo 'dependabot[bot]' ;;
  */commits/f6) echo releaser ;;
  */commits/g7) echo alice ;;
  */commits/h8) echo carol ;;
  */commits/i9) echo maint ;;
  */commits/j0) echo dave ;;
  'repos/PasarGuard/panel/commits?author=maint&per_page=1')
    [[ -z "${FAIL_UPSTREAM_LOOKUP:-}" ]] || exit 1
    echo 1 ;;
  'repos/PasarGuard/panel/commits?author='*'&per_page=1')
    [[ -z "${FAIL_UPSTREAM_LOOKUP:-}" ]] || exit 1
    echo 0 ;;
esac
"""


def _contributors_step() -> str:
    text = WORKFLOW.read_text()
    return text.split("            - name: Add GitHub contributors\n", 1)[1].split("\n            - name: ", 1)[0]


def _contributors_from_workflow() -> str:
    body = _contributors_step().split("              run: |\n", 1)[1]
    lines = [ln[18:] if ln.startswith(" " * 18) else ln.strip() for ln in body.splitlines()]
    return "set -euo pipefail\n" + "\n".join(lines) + "\n"


def _run_contributors(tmp_path, extra_env=None):
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
        "UPSTREAM_REPOSITORY": "PasarGuard/panel",
        "PREVIOUS_TAG": "v9.9.8",
        "CURRENT_TAG": "v9.9.9",
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", "contributors.sh"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )


@pytest.fixture
def contributors_run(tmp_path):
    result = _run_contributors(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path


def test_contributors_step_never_runs_commit_subjects(contributors_run):
    assert not (contributors_run / "PWNED").exists()
    assert not (contributors_run / "PWNED2").exists()


def test_contributors_step_lists_human_logins_once(contributors_run):
    notes = (contributors_run / "CHANGELOG.md").read_text()
    assert notes == "## v9.9.9\n\n### Contributors\n\n- @alice\n- @Bob\n- @dave\n"


def test_contributors_step_appends_no_address(contributors_run):
    assert ADDRESS.search((contributors_run / "CHANGELOG.md").read_text()) is None


def test_contributors_step_skips_a_commit_with_a_cherry_pick_trailer(contributors_run):
    assert "carol" not in (contributors_run / "CHANGELOG.md").read_text()


def test_contributors_step_skips_an_upstream_author(contributors_run):
    assert "maint" not in (contributors_run / "CHANGELOG.md").read_text()


def test_contributors_step_asks_the_upstream_panel_repository():
    assert "UPSTREAM_REPOSITORY: PasarGuard/panel\n" in _contributors_step()


def test_contributors_step_fails_instead_of_listing_when_the_upstream_lookup_fails(tmp_path):
    result = _run_contributors(tmp_path, {"FAIL_UPSTREAM_LOOKUP": "1"})
    assert result.returncode != 0
    assert (tmp_path / "CHANGELOG.md").read_text() == "## v9.9.9\n"
