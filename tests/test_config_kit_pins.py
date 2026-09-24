import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".github" / "config-kits.lock"
SCRIPT = ROOT / "scripts" / "fetch_config_kits.sh"
KIT_WORKFLOWS = ("build.yml", "build-dev.yml", "dashboard-smoke.yml")


def _pins() -> dict[str, str]:
    pins = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.strip():
            kit, ref = line.split()
            pins[kit] = ref
    return pins


def test_every_dashboard_kit_is_pinned_to_a_full_commit():
    package = json.loads((ROOT / "dashboard" / "package.json").read_text(encoding="utf-8"))
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    kits = {spec.removeprefix("file:../../") for spec in dependencies.values() if spec.startswith("file:../../")}
    pins = _pins()
    assert set(pins) == kits
    for kit, ref in pins.items():
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (kit, ref)


@pytest.mark.parametrize("workflow", KIT_WORKFLOWS)
def test_each_workflow_fetches_the_kits_only_through_the_pinned_script(workflow):
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    assert "scripts/fetch_config_kits.sh" in text
    assert "KITS_OWNER: ${{ github.repository_owner }}" in text
    assert "config-kit.git" not in text and "$kit.git" not in text
    assert not re.search(r"bun install(?! --frozen-lockfile)", text), workflow


def test_the_script_installs_every_kit_from_its_lockfile():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "bun install --frozen-lockfile" in script
    assert not re.search(r"bun install(?! --frozen-lockfile)", script)
    assert "--depth" not in script


def test_the_script_refuses_a_kit_that_is_not_pinned_to_a_full_commit(tmp_path):
    lock = tmp_path / "kits.lock"
    lock.write_text("core-kit main\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "KITS_OWNER": "example", "KITS_LOCK": str(lock), "KITS_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "core-kit is not pinned to a full commit" in result.stdout
    assert not (tmp_path / "core-kit").exists()
