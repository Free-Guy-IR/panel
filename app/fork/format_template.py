from collections import defaultdict
from string import Formatter

from app.utils.helpers import MAX_FORMAT_SPEC_NUMBER, has_oversized_format_spec

FORMAT_CONVERSIONS = (None, "s", "r", "a")


def is_plain_field(field: str, conversion: str | None) -> bool:
    return (
        bool(field)
        and not field.isdigit()
        and "." not in field
        and "[" not in field
        and conversion in FORMAT_CONVERSIONS
    )


def check_template_value(value: str) -> str:
    if has_oversized_format_spec(value):
        raise ValueError(
            f"Invalid formatting variables: width and precision must be numbers up to {MAX_FORMAT_SPEC_NUMBER}"
        )
    fields = [(field, conversion) for _, field, _, conversion in Formatter().parse(value) if field is not None]
    if not all(is_plain_field(field, conversion) for field, conversion in fields):
        raise ValueError("Invalid formatting variables: use plain variable names such as {USERNAME}")
    try:
        value.format_map(defaultdict(str))
    except ValueError, TypeError:
        raise ValueError("Invalid formatting variables")
    return value
