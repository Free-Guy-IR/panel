from sqlalchemy import String, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JWT, System
from app.models.stats import Period

MYSQL_FORMATS = {
    Period.minute: "%Y-%m-%d %H:%i:00",
    Period.hour: "%Y-%m-%d %H:00:00",
    Period.day: "%Y-%m-%d",
    Period.month: "%Y-%m-01",
}
SQLITE_FORMATS = {
    Period.minute: "%Y-%m-%d %H:%M:00",
    Period.hour: "%Y-%m-%d %H:00:00",
    Period.day: "%Y-%m-%d",
    Period.month: "%Y-%m-01",
}


def _build_trunc_expression(db: AsyncSession, period: Period, column):
    dialect = db.bind.dialect.name

    """Builds the appropriate truncation SQL expression based on dialect and period."""
    if dialect == "postgresql":
        return func.date_trunc(period.value, column)
    elif dialect == "mysql":
        return func.date_format(column, MYSQL_FORMATS[period.value])
    elif dialect == "sqlite":
        return func.strftime(SQLITE_FORMATS[period.value], column)

    raise ValueError(f"Unsupported dialect: {dialect}")


def get_datetime_add_expression(db: AsyncSession, datetime_column, seconds: int):
    """
    Get database-specific datetime addition expression
    """
    dialect = db.bind.dialect.name
    if dialect == "mysql":
        return func.date_add(datetime_column, text("INTERVAL :seconds SECOND").bindparams(seconds=seconds))
    elif dialect == "postgresql":
        return datetime_column + func.make_interval(0, 0, 0, 0, 0, 0, seconds)
    elif dialect == "sqlite":
        return func.datetime(func.strftime("%s", datetime_column) + seconds, "unixepoch")

    raise ValueError(f"Unsupported dialect: {dialect}")


def json_extract(db: AsyncSession, column, path: str):
    """
    Args:
        column: The JSON column in your model
        path: JSON path (e.g., '$.theme')
    """
    dialect = db.bind.dialect.name
    match dialect:
        case "postgresql":
            keys = path.replace("$.", "").split(".")
            expr = column
            for key in keys:
                expr = expr.op("->>")(key) if key == keys[-1] else expr.op("->")(key)
            return expr.cast(String)
        case "mysql":
            return func.json_unquote(func.json_extract(column, path)).cast(String)
        case "sqlite":
            return func.json_extract(column, path).cast(String)


def build_json_proxy_settings_search_condition(db: AsyncSession, column, value: str):
    """
    Builds a condition to search JSON column for UUIDs or passwords.
    Supports PostgresSQL, MySQL, SQLite.
    """
    return or_(
        *[
            json_extract(db, column, field) == value
            for field in ("$.vmess.id", "$.vless.id", "$.trojan.password", "$.shadowsocks.password")
        ],
    )


async def get_system_usage(db: AsyncSession) -> System:
    """
    Retrieves system usage information.

    Args:
        db (AsyncSession): Database session.

    Returns:
        System: System usage information.
    """
    return (await db.execute(select(System))).scalar_one_or_none()


async def get_jwt_secret_key(db: AsyncSession) -> str:
    """
    Retrieves the JWT secret key.

    Args:
        db (AsyncSession): Database session.

    Returns:
        str: JWT secret key.
    """
    return (await db.execute(select(JWT))).scalar_one_or_none().secret_key
