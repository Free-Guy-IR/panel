from string import Formatter

from app.utils.helpers import MAX_FORMAT_SPEC_NUMBER, has_oversized_format_spec

MAX_CUSTOM_VARIABLES = 100
MAX_FORMAT_GROWTH = 4096
_CONVERT = {None: lambda value: value, "s": str, "r": repr, "a": ascii}


def is_plain_field(field: str, conversion: str | None) -> bool:
    return bool(field) and not field.isdigit() and "." not in field and "[" not in field and conversion in _CONVERT


def is_character_spec(spec: str) -> bool:
    return spec.endswith("c")


def is_unparseable(value: str) -> bool:
    try:
        for _ in Formatter().parse(value):
            pass
    except ValueError:
        return True
    return False


def check_template_value(value: str) -> str:
    if is_unparseable(value):
        raise ValueError('Invalid formatting variables: every "{" and "}" needs a partner; write {{ or }} for a brace')
    if has_oversized_format_spec(value):
        raise ValueError(
            f"Invalid formatting variables: width and precision must be numbers up to {MAX_FORMAT_SPEC_NUMBER}"
        )
    fields = [(field, spec, conversion) for _, field, spec, conversion in Formatter().parse(value) if field is not None]
    if not all(is_plain_field(field, conversion) for field, _, conversion in fields):
        raise ValueError("Invalid formatting variables: use plain variable names such as {USERNAME}")
    if any(is_character_spec(spec) for _, spec, _ in fields):
        raise ValueError("Invalid formatting variables")
    for _, spec, conversion in fields:
        for sample in ("",) if conversion else ("", 0, 0.0):
            try:
                format(sample, spec)
            except ValueError, TypeError:
                continue
            break
        else:
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
        if not is_plain_field(field, conversion) or is_character_spec(spec):
            return template
        piece = format(_CONVERT[conversion](variables[field]), spec)
        length += len(piece)
        if length > limit:
            return template
        pieces.append(piece)
    return "".join(pieces)
