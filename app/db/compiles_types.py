import json
from sqlalchemy import JSON, String, Numeric, ARRAY, TypeDecorator, Enum
from sqlalchemy.sql.expression import FunctionElement
from sqlalchemy.ext.compiler import compiles


class CaseSensitiveString(String):
    def __init__(self, length=None):
        super(CaseSensitiveString, self).__init__(length)


# Modify how this type is handled for each dialect
@compiles(CaseSensitiveString, "sqlite")
def compile_cs_sqlite(element, compiler, **kw):
    return f"VARCHAR({element.length}) COLLATE BINARY"  # BINARY is case-sensitive in SQLite


@compiles(CaseSensitiveString, "postgresql")
def compile_cs_postgresql(element, compiler, **kw):
    return f'VARCHAR({element.length}) COLLATE "C"'  # "C" collation is case-sensitive


@compiles(CaseSensitiveString, "mysql")
def compile_cs_mysql(element, compiler, **kw):
    return f"VARCHAR({element.length}) COLLATE utf8mb4_bin"  # utf8mb4_bin is case-sensitive


class EnumArray(TypeDecorator):
    """Custom SQLAlchemy type to handle Enum lists across PostgreSQL, MySQL, and SQLite"""
    impl = JSON  # Default to JSON for MySQL/SQLite
    
    def __init__(self, enum_cls):
        super().__init__()
        self.enum_cls = enum_cls
    
    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            # Extract enum values from the SQLAlchemy Enum type
            if hasattr(self.enum_cls, "enums"):
                # If it's a SQLAlchemy Enum
                enum_values = self.enum_cls.enums
                enum_name = self.enum_cls.name
            else:
                # If it's a Python enum.Enum
                enum_values = [e.value for e in self.enum_cls.__members__.values()]
                enum_name = self.enum_cls.__name__.lower()
                
            return dialect.type_descriptor(ARRAY(Enum(*enum_values, name=enum_name)))
        return dialect.type_descriptor(JSON)  # Use JSON for MySQL & SQLite
    
    def process_bind_param(self, value, dialect):
        """Convert Enum list to appropriate format for storage"""
        if value is None:
            return None
        if dialect.name == "postgresql":
            return [v.value for v in value]  # Store as native PG array
        return json.dumps([v.value for v in value])  # Store as JSON for MySQL/SQLite
    
    def process_result_value(self, value, dialect):
        """Convert stored values back to Enum list"""
        if value is None:
            return None
        if dialect.name == "postgresql":
            return [self.enum_cls(v) for v in value]  # PostgreSQL returns a list directly
        if isinstance(value, str):  # Ensure JSON parsing only if it's a string
            return [self.enum_cls(v) for v in json.loads(value)]
        return [self.enum_cls(v) for v in value]  # If it's already a list, return as Enums


class DaysDiff(FunctionElement):
    type = Numeric()
    name = "days_diff"
    inherit_cache = True


@compiles(DaysDiff, "postgresql")
def compile_days_diff_postgresql(element, compiler, **kw):
    return "EXTRACT(EPOCH FROM (expire - CURRENT_TIMESTAMP)) / 86400"


@compiles(DaysDiff, "mysql")
def compile_days_diff_mysql(element, compiler, **kw):
    return "DATEDIFF(expire, UTC_TIMESTAMP())"


@compiles(DaysDiff, "sqlite")
def compile_days_diff_sqlite(element, compiler, **kw):
    return "(julianday(expire) - julianday('now'))"
