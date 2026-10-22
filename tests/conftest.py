import asyncio
import os
import sys
import tempfile
import warnings
from collections import Counter
from pathlib import Path

import pytest
from aiorwlock import RWLock
from dotenv import dotenv_values
from pydantic import PydanticDeprecatedSince20
from sqlalchemy.engine import make_url

# Add the project root directory to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

# Each xdist worker gets its own SQLite file. The app engine reads SQLALCHEMY_DATABASE_URL when config is imported
# below, so the per-worker URL must be set here, for it and for the test helpers' DATABASE_URL alike: with only
# DATABASE_URL changed, API requests use the shared, unmigrated app database ("no such table: jwt").
# xdist sets PYTEST_XDIST_WORKER before it imports this module. TEST_FROM and DATABASE_URL are read like the test
# settings read them (tests/api/__init__.py): the environment first, then .env, names in any case.
_dotenv = {key.upper(): value for key, value in dotenv_values(".env").items() if value is not None}


def _test_setting(name: str, default: str) -> str:
    environ = {key.upper(): value for key, value in os.environ.items()}
    return environ.get(name, _dotenv.get(name, default))


_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER")
_test_from = _test_setting("TEST_FROM", "local")
_requested_database_url = make_url(_test_setting("DATABASE_URL", "sqlite+aiosqlite:///./test.db"))
_worker_database_dir: Path | None = None
if (
    _xdist_worker
    and _test_from == "local"
    and _requested_database_url.get_backend_name() == "sqlite"
    and _requested_database_url.database not in (None, "", ":memory:")
):
    _worker_database_dir = Path(tempfile.mkdtemp(prefix=f"pasarguard-pytest-{_xdist_worker}-"))
    _worker_database_url = str(_requested_database_url.set(database=str(_worker_database_dir / "test.db")))
    os.environ["DATABASE_URL"] = _worker_database_url
    os.environ["SQLALCHEMY_DATABASE_URL"] = _worker_database_url

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


def pytest_addoption(parser):
    group = parser.getgroup("sharding")
    group.addoption("--shard-index", type=int, help="Zero based CI shard index")
    group.addoption("--shard-count", type=int, help="Number of independent CI shards")


def pytest_collection_modifyitems(config, items):
    index = config.getoption("--shard-index")
    count = config.getoption("--shard-count")
    if index is None and count is None:
        return
    if count is None or index is None or count < 1 or not 0 <= index < count:
        raise pytest.UsageError("Set --shard-index between 0 and --shard-count minus 1")

    # Keep tests from each file together and spread the largest files first.
    file_counts = Counter(item.path for item in items)
    assigned = {}
    loads = [0] * count
    for path, size in sorted(file_counts.items(), key=lambda entry: (-entry[1], str(entry[0]))):
        shard = min(range(count), key=lambda candidate: loads[candidate])
        assigned[path] = shard
        loads[shard] += size

    selected = [item for item in items if assigned[item.path] == index]
    deselected = [item for item in items if assigned[item.path] != index]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected


def pytest_configure(config):
    """Refuse parallel runs that cannot get a per-worker SQLite database (set up at import, above)."""
    if getattr(config, "workerinput", None) is None:
        return

    if _test_from != "local":
        raise pytest.UsageError("Parallel tests require TEST_FROM=local and SQLite; use serial tests for CI databases")
    if _worker_database_dir is None:
        raise pytest.UsageError("Parallel tests require a file backed SQLite DATABASE_URL")


def pytest_unconfigure(config):
    if _worker_database_dir is not None and _worker_database_dir.exists():
        for file in _worker_database_dir.iterdir():
            file.unlink()
        _worker_database_dir.rmdir()


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
