import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.version import Version

FIXED_FLOORS = {
    "aiohttp": Version("3.14.4"),
    "yarl": Version("1.25.1"),
}

LOCK_PATH = Path(__file__).resolve().parents[1] / "uv.lock"


def _locked_versions() -> dict[str, Version]:
    lock = tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))
    return {
        package["name"]: Version(package["version"]) for package in lock["package"] if package["name"] in FIXED_FLOORS
    }


@pytest.mark.parametrize("name", sorted(FIXED_FLOORS))
def test_installed_version_is_at_or_above_the_fixed_release(name):
    assert Version(version(name)) >= FIXED_FLOORS[name]


@pytest.mark.parametrize("name", sorted(FIXED_FLOORS))
def test_locked_version_is_at_or_above_the_fixed_release(name):
    locked = _locked_versions()
    assert name in locked
    assert locked[name] >= FIXED_FLOORS[name]
