import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.schema import CreateTable

from app.db.models import AdminPasskey


@pytest.mark.parametrize(
    ("dialect", "column"),
    [
        (mysql.dialect(), "credential_id VARBINARY(1024) NOT NULL"),
        (MariaDBDialect(), "credential_id VARBINARY(1024) NOT NULL"),
        (postgresql.dialect(), "credential_id BYTEA NOT NULL"),
        (sqlite.dialect(), "credential_id BLOB NOT NULL"),
    ],
    ids=["mysql", "mariadb", "postgresql", "sqlite"],
)
def test_the_passkey_credential_id_stays_a_unique_indexable_column(dialect, column):
    ddl = str(CreateTable(AdminPasskey.__table__).compile(dialect=dialect))
    lines = [line.strip().rstrip(",") for line in ddl.splitlines()]
    assert column in lines
    assert "CONSTRAINT uq_admin_passkeys_credential_id UNIQUE (credential_id)" in lines
