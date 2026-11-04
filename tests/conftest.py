import asyncio
import os
import sys
import warnings
from pathlib import Path

import pytest
from aiorwlock import RWLock
from pydantic import PydanticDeprecatedSince20

# Add the project root directory to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

# Override settings for tests
from config import auth_settings, runtime_settings, server_settings

runtime_settings.testing = True
runtime_settings.debug = True
server_settings.proxy_headers = True
server_settings.forwarded_allow_ips = "*"
auth_settings.sudoers["testadmin"] = "testadmin"


def _directory_grouped(args: list[str], invocation_dir: Path) -> list[str]:
    first_seen: dict[Path, int] = {}
    keys = []
    for index, arg in enumerate(args):
        target = (invocation_dir / arg.split("::", 1)[0]).resolve()
        directories = [*reversed(target.parents)]
        if target.is_dir():
            directories.append(target)
        for directory in directories:
            first_seen.setdefault(directory, index)
        keys.append((*(first_seen[directory] for directory in directories), index))
    return [arg for _, arg in sorted(zip(keys, args), key=lambda pair: pair[0])]


def pytest_sessionstart(session):
    args = session.config.args
    args[:] = _directory_grouped(args, session.config.invocation_params.dir)


# Filter out all warnings
@pytest.fixture(autouse=True)
def ignore_all_warnings():
    warnings.filterwarnings("ignore")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=PydanticDeprecatedSince20)
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=RuntimeWarning)


@pytest.fixture(autouse=True)
def loop_bound_node_state_per_test_loop(monkeypatch):
    node = sys.modules.get("app.node")
    if node is not None:
        monkeypatch.setattr(node.node_manager, "_lock", RWLock(fast=True))
    bridge_memory = sys.modules.get("app.node.nats_memory")
    if bridge_memory is not None:
        for name in ("_nc", "_user_sync_kv", "_lifecycle_kv", "_user_sync_store", "_lifecycle_coordinator"):
            monkeypatch.setattr(bridge_memory, name, None)
        monkeypatch.setattr(bridge_memory, "_init_lock", asyncio.Lock())


def _fresh_host_manager_locks() -> None:
    hosts = sys.modules.get("app.core.hosts")
    if hosts is not None:
        hosts.host_manager._lock = asyncio.Lock()
        hosts.host_manager._publish_lock = asyncio.Lock()


@pytest.fixture(autouse=True)
def host_manager_locks_are_fresh_per_test():
    _fresh_host_manager_locks()
    yield
    _fresh_host_manager_locks()


@pytest.fixture
def refuse_submitted_collisions(monkeypatch):
    from config import SubmittedSecretCollision, proxy_secret_settings

    monkeypatch.setattr(proxy_secret_settings, "submitted_collision", SubmittedSecretCollision.refuse)


@pytest.fixture
def replace_submitted_collisions(monkeypatch):
    from config import SubmittedSecretCollision, proxy_secret_settings

    monkeypatch.setattr(proxy_secret_settings, "submitted_collision", SubmittedSecretCollision.replace)
