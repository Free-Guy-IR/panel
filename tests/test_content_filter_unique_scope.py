import logging
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVISION = "c7f1a9d3e604"
PREVIOUS_REVISION = "b8e6f2a4c517"


def _migrate(db_path: Path, revision: str) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        connection.commit()
    return engine


def _seed_profile(connection: sa.Connection, profile_id: int, name: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO content_filter_profiles (id, name, created_at, updated_at, strict_mode) "
            "VALUES (:id, :name, :now, :now, 1)"
        ),
        {"id": profile_id, "name": name, "now": "2026-01-01 00:00:00"},
    )


def _insert_assignment(
    connection: sa.Connection,
    node_id: int | None,
    inbound_tag: str,
    profile_id: int = 1,
    is_enabled: bool = True,
    enforced: bool = False,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO content_filter_assignments (profile_id, node_id, inbound_tag, is_enabled, enforced) "
            "VALUES (:profile_id, :node_id, :inbound_tag, :is_enabled, :enforced)"
        ),
        {
            "profile_id": profile_id,
            "node_id": node_id,
            "inbound_tag": inbound_tag,
            "is_enabled": 1 if is_enabled else 0,
            "enforced": 1 if enforced else 0,
        },
    )


class _WarningCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _migrate_capturing_warnings(db_path: Path, revision: str) -> tuple[sa.Engine, list[str]]:
    collector = _WarningCollector()
    migration_logger = logging.getLogger("alembic.runtime.migration")
    previous_level = migration_logger.level
    migration_logger.setLevel(logging.WARNING)
    migration_logger.addHandler(collector)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
            connection.commit()
    finally:
        migration_logger.removeHandler(collector)
        migration_logger.setLevel(previous_level)
    return engine, collector.messages


def _survivors_after_migration(db_path: Path, seed) -> list[tuple]:
    engine = _migrate(db_path, PREVIOUS_REVISION)
    with engine.begin() as connection:
        _seed_profile(connection, 1, "profile-one")
        seed(connection)
    engine.dispose()

    engine = _migrate(db_path, REVISION)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT id, node_id, inbound_tag, is_enabled, enforced FROM content_filter_assignments ORDER BY id")
        ).fetchall()
    engine.dispose()
    return [tuple(row) for row in rows]


@pytest.fixture(scope="module")
def migrated_engine(tmp_path_factory: pytest.TempPathFactory) -> sa.Engine:
    engine = _migrate(tmp_path_factory.mktemp("unique-scope") / "head.db", REVISION)
    with engine.begin() as connection:
        _seed_profile(connection, 1, "profile-one")
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def legacy_engine(tmp_path_factory: pytest.TempPathFactory) -> sa.Engine:
    engine = _migrate(tmp_path_factory.mktemp("unique-scope-legacy") / "previous.db", PREVIOUS_REVISION)
    with engine.begin() as connection:
        _seed_profile(connection, 1, "profile-one")
    yield engine
    engine.dispose()


def test_two_fleet_wide_rows_for_one_tag_are_refused_by_the_database(migrated_engine: sa.Engine):
    with migrated_engine.begin() as connection:
        _insert_assignment(connection, None, "in-refused")
    with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
        _insert_assignment(connection, None, "in-refused")
    with migrated_engine.connect() as connection:
        count = connection.execute(
            sa.text("SELECT COUNT(*) FROM content_filter_assignments WHERE node_id IS NULL AND inbound_tag = :tag"),
            {"tag": "in-refused"},
        ).scalar_one()
    assert count == 1


def test_the_whole_node_scope_is_covered_too(migrated_engine: sa.Engine):
    with migrated_engine.begin() as connection:
        _insert_assignment(connection, None, "")
    with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
        _insert_assignment(connection, None, "")


def test_the_same_tag_on_two_different_nodes_is_still_allowed(migrated_engine: sa.Engine):
    with migrated_engine.begin() as connection:
        _insert_assignment(connection, 11, "in-pinned")
        _insert_assignment(connection, 12, "in-pinned")
    with migrated_engine.connect() as connection:
        count = connection.execute(
            sa.text("SELECT COUNT(*) FROM content_filter_assignments WHERE inbound_tag = :tag"),
            {"tag": "in-pinned"},
        ).scalar_one()
    assert count == 2


def test_a_pinned_row_and_a_fleet_wide_row_may_share_a_tag(migrated_engine: sa.Engine):
    with migrated_engine.begin() as connection:
        _insert_assignment(connection, None, "in-mixed")
        _insert_assignment(connection, 13, "in-mixed")
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT node_id, node_scope FROM content_filter_assignments "
                "WHERE inbound_tag = :tag ORDER BY node_scope"
            ),
            {"tag": "in-mixed"},
        ).fetchall()
    assert [tuple(row) for row in rows] == [(None, 0), (13, 13)]


def test_repeating_the_same_pinned_scope_is_still_refused(migrated_engine: sa.Engine):
    with migrated_engine.begin() as connection:
        _insert_assignment(connection, 14, "in-pinned-twice")
    with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
        _insert_assignment(connection, 14, "in-pinned-twice")


def test_the_database_accepted_duplicates_before_this_migration(legacy_engine: sa.Engine):
    with legacy_engine.begin() as connection:
        _insert_assignment(connection, None, "in-legacy")
        _insert_assignment(connection, None, "in-legacy")
    with legacy_engine.connect() as connection:
        count = connection.execute(
            sa.text("SELECT COUNT(*) FROM content_filter_assignments WHERE node_id IS NULL AND inbound_tag = :tag"),
            {"tag": "in-legacy"},
        ).scalar_one()
    assert count == 2


def test_the_migration_keeps_the_lowest_id_of_each_duplicated_scope(tmp_path: Path):
    db_path = tmp_path / "duplicates.db"
    engine = _migrate(db_path, PREVIOUS_REVISION)
    with engine.begin() as connection:
        _seed_profile(connection, 1, "profile-one")
        _seed_profile(connection, 2, "profile-two")
        _insert_assignment(connection, None, "in-dup", profile_id=1)
        _insert_assignment(connection, None, "in-dup", profile_id=2)
        _insert_assignment(connection, None, "in-dup", profile_id=1)
        _insert_assignment(connection, None, "", profile_id=1)
        _insert_assignment(connection, None, "", profile_id=2)
        _insert_assignment(connection, None, "in-kept", profile_id=1)
        _insert_assignment(connection, 21, "in-dup", profile_id=1)
    with engine.connect() as connection:
        before = connection.execute(
            sa.text("SELECT id, node_id, inbound_tag FROM content_filter_assignments ORDER BY id")
        ).fetchall()
    assert len(before) == 7
    engine.dispose()

    engine = _migrate(db_path, REVISION)
    with engine.connect() as connection:
        after = connection.execute(
            sa.text("SELECT id, profile_id, node_id, inbound_tag FROM content_filter_assignments ORDER BY id")
        ).fetchall()
    assert [tuple(row) for row in after] == [
        (1, 1, None, "in-dup"),
        (4, 1, None, ""),
        (6, 1, None, "in-kept"),
        (7, 1, 21, "in-dup"),
    ]

    with pytest.raises(IntegrityError), engine.begin() as connection:
        _insert_assignment(connection, None, "in-dup")
    engine.dispose()


def test_the_migration_keeps_the_enabled_row_over_an_older_disabled_one(tmp_path: Path):
    def seed(connection: sa.Connection) -> None:
        _insert_assignment(connection, None, "in-shadowed", profile_id=1, is_enabled=False)
        _insert_assignment(connection, None, "in-shadowed", profile_id=1, is_enabled=True)

    assert _survivors_after_migration(tmp_path / "enabled-wins.db", seed) == [(2, None, "in-shadowed", 1, 0)]


def test_a_pinned_row_sharing_the_tag_is_left_alone_while_the_fleet_wide_duplicate_is_pruned(tmp_path: Path):
    def seed(connection: sa.Connection) -> None:
        _insert_assignment(connection, None, "in-mixed-dup", is_enabled=False)
        _insert_assignment(connection, 31, "in-mixed-dup", is_enabled=False)
        _insert_assignment(connection, None, "in-mixed-dup", is_enabled=True)

    assert _survivors_after_migration(tmp_path / "pinned-untouched.db", seed) == [
        (2, 31, "in-mixed-dup", 0, 0),
        (3, None, "in-mixed-dup", 1, 0),
    ]


def test_two_enabled_rows_of_one_scope_leave_the_lowest_id(tmp_path: Path):
    def seed(connection: sa.Connection) -> None:
        _insert_assignment(connection, None, "in-both-live")
        _insert_assignment(connection, None, "in-both-live")

    assert _survivors_after_migration(tmp_path / "lowest-id-wins.db", seed) == [(1, None, "in-both-live", 1, 0)]


def test_an_enforced_row_outranks_an_enabled_but_unenforced_one(tmp_path: Path):
    def seed(connection: sa.Connection) -> None:
        _insert_assignment(connection, None, "in-enforced", is_enabled=True, enforced=False)
        _insert_assignment(connection, None, "in-enforced", is_enabled=True, enforced=True)

    assert _survivors_after_migration(tmp_path / "enforced-wins.db", seed) == [(2, None, "in-enforced", 1, 1)]


def test_being_enabled_outranks_being_enforced(tmp_path: Path):
    def seed(connection: sa.Connection) -> None:
        _insert_assignment(connection, None, "in-precedence", is_enabled=False, enforced=True)
        _insert_assignment(connection, None, "in-precedence", is_enabled=True, enforced=False)

    assert _survivors_after_migration(tmp_path / "enabled-beats-enforced.db", seed) == [
        (2, None, "in-precedence", 1, 0)
    ]


def test_every_dropped_row_is_named_in_a_warning(tmp_path: Path):
    db_path = tmp_path / "logged.db"
    engine = _migrate(db_path, PREVIOUS_REVISION)
    with engine.begin() as connection:
        _seed_profile(connection, 1, "profile-one")
        _insert_assignment(connection, None, "in-logged", is_enabled=False)
        _insert_assignment(connection, None, "in-logged", is_enabled=True)
        _insert_assignment(connection, None, "in-logged", is_enabled=False, enforced=True)
    engine.dispose()

    engine, messages = _migrate_capturing_warnings(db_path, REVISION)
    engine.dispose()

    dropped = [message for message in messages if "dropping duplicate assignment" in message]
    assert len(dropped) == 2
    assert any("id=1 (keeping id=2)" in message and "is_enabled=0" in message for message in dropped)
    assert any("id=3 (keeping id=2)" in message and "enforced=1" in message for message in dropped)
    assert any("removed 2 duplicate assignment row(s)" in message for message in messages)


def test_the_migration_is_reversible_and_re_appliable(tmp_path: Path):
    db_path = tmp_path / "roundtrip.db"
    engine = _migrate(db_path, REVISION)
    with engine.begin() as connection:
        _seed_profile(connection, 1, "profile-one")
        _insert_assignment(connection, None, "in-roundtrip")
    engine.dispose()

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, PREVIOUS_REVISION)
        connection.commit()
    with engine.connect() as connection:
        names = {column["name"] for column in sa.inspect(engine).get_columns("content_filter_assignments")}
        surviving = connection.execute(sa.text("SELECT COUNT(*) FROM content_filter_assignments")).scalar_one()
    assert "node_scope" not in names
    assert surviving == 1
    engine.dispose()

    engine = _migrate(db_path, REVISION)
    with engine.connect() as connection:
        names = {column["name"] for column in sa.inspect(engine).get_columns("content_filter_assignments")}
        indexes = {index["name"] for index in sa.inspect(engine).get_indexes("content_filter_assignments")}
    assert "node_scope" in names
    assert "uq_content_filter_assignments_scope_inbound" in indexes
    engine.dispose()
