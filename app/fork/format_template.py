from string import Formatter

from app.utils.helpers import MAX_FORMAT_SPEC_NUMBER, has_oversized_format_spec

MAX_CUSTOM_VARIABLES = 100
MAX_FORMAT_GROWTH = 4096
_CONVERT = {None: lambda value: value, "s": str, "r": repr, "a": ascii}
_RENDERED_SAMPLES = {"DAYS_LEFT": ("", 0), "USAGE_PERCENTAGE": ("", 0.0)}


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


def has_stray_brace(value: str) -> bool:
    depth = 0
    index = 0
    while index < len(value):
        char = value[index]
        if depth == 0 and char in "{}" and value.startswith(char * 2, index):
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return True
            depth -= 1
        index += 1
    return depth != 0


def check_template_value(value: str) -> str:
    if is_unparseable(value):
        if has_stray_brace(value):
            raise ValueError(
                'Invalid formatting variables: every "{" and "}" needs a partner; write {{ or }} for a brace'
            )
        raise ValueError("Invalid formatting variables")
    if has_oversized_format_spec(value):
        raise ValueError(
            f"Invalid formatting variables: width and precision must be numbers up to {MAX_FORMAT_SPEC_NUMBER}"
        )
    fields = [(field, spec, conversion) for _, field, spec, conversion in Formatter().parse(value) if field is not None]
    if not all(is_plain_field(field, conversion) for field, _, conversion in fields):
        raise ValueError("Invalid formatting variables: use plain variable names such as {USERNAME}")
    if any(is_character_spec(spec) for _, spec, _ in fields):
        raise ValueError("Invalid formatting variables")
    for field, spec, conversion in fields:
        for sample in ("",) if conversion else _RENDERED_SAMPLES.get(field, ("",)):
            try:
                format(sample, spec)
            except ValueError, TypeError:
                raise ValueError("Invalid formatting variables") from None
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
