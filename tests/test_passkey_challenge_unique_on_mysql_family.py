import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.schema import CreateTable

from app.db.models import PasskeyChallenge


@pytest.mark.parametrize(
    ("dialect", "column"),
    [
        (mysql.dialect(), "challenge VARBINARY(128) NOT NULL"),
        (MariaDBDialect(), "challenge VARBINARY(128) NOT NULL"),
        (postgresql.dialect(), "challenge BYTEA NOT NULL"),
        (sqlite.dialect(), "challenge BLOB NOT NULL"),
    ],
    ids=["mysql", "mariadb", "postgresql", "sqlite"],
)
def test_the_single_use_challenge_store_keeps_a_unique_indexable_challenge_column(dialect, column):
    ddl = str(CreateTable(PasskeyChallenge.__table__).compile(dialect=dialect))
    lines = [line.strip().rstrip(",") for line in ddl.splitlines()]
    assert column in lines
    assert "CONSTRAINT uq_passkey_challenges_challenge UNIQUE (challenge)" in lines
