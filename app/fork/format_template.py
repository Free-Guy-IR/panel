from collections import defaultdict
from string import Formatter

from app.utils.helpers import MAX_FORMAT_SPEC_NUMBER, has_oversized_format_spec

MAX_CUSTOM_VARIABLES = 100
MAX_FORMAT_GROWTH = 4096
_CONVERT = {None: lambda value: value, "s": str, "r": repr, "a": ascii}


def is_plain_field(field: str, conversion: str | None) -> bool:
    return bool(field) and not field.isdigit() and "." not in field and "[" not in field and conversion in _CONVERT


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


def format_bounded(template: str, variables) -> str:
    if has_oversized_format_spec(template):
        return template
    limit = len(template) + MAX_FORMAT_GROWTH
    pieces: list[str] = []
    length = 0
    for literal, field, spec, conversion in Formatter().parse(template):
        pieces.append(literal)
        length += len(literal)
        if field is None:
            continue
        if not is_plain_field(field, conversion):
            return template
        piece = format(_CONVERT[conversion](variables[field]), spec)
        length += len(piece)
        if length > limit:
            return template
        pieces.append(piece)
    return "".join(pieces)
