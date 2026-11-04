MYSQL_FAMILY = frozenset({"mysql", "mariadb"})


def sql_family(dialect_name: str) -> str:
    return "mysql" if dialect_name in MYSQL_FAMILY else dialect_name
