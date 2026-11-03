import logging
import operator
import re
import sys
from collections.abc import Mapping
from typing import Self

from app.fork.operation.node_messages import scrub_credentials

_EXCEPTION_FORMATTER = logging.Formatter()
_FORMAT_EXCEPTION = logging.Formatter.formatException
_SCRUBBED_STATE = "_credential_scrub_state"
_PLAIN_TYPES = (int, float, bool, type(None))
_DIRECTIVE = re.compile(
    r"%(?:\((?P<key>[^()]*)\))?[-+ #0]*(?P<width>\*|\d+)?(?:\.(?P<precision>\*|\d*))?[hlL]?(?P<conversion>.)",
    re.DOTALL,
)
_TEXT_CONVERSIONS = {"s": str, "r": repr, "a": ascii}
_INDEX_CONVERSIONS = frozenset("oxXc")
_INT_CONVERSIONS = frozenset("diu*")
_FLOAT_CONVERSIONS = frozenset("eEfFgG")


class _FrozenText(str):
    def __new__(cls, text: str, shown: str | None = None) -> Self:
        frozen = super().__new__(cls, text)
        frozen._shown = shown
        return frozen

    def __repr__(self) -> str:
        return str(self) if self._shown is None else self._shown


class _FrozenInt(int):
    def __new__(cls, value: int, text: str = "", shown: str | None = None) -> Self:
        frozen = super().__new__(cls, value)
        frozen._text, frozen._shown = text, shown
        return frozen

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._text if self._shown is None else self._shown


class _FrozenFloat(float):
    def __new__(cls, value: float, text: str = "", shown: str | None = None) -> Self:
        frozen = super().__new__(cls, value)
        frozen._text, frozen._shown = text, shown
        return frozen

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._text if self._shown is None else self._shown


def _is_plain(argument: object) -> bool:
    return type(argument) in _PLAIN_TYPES


def _rendered(argument: object, render) -> str:
    try:
        return render(argument)
    except Exception:
        return object.__repr__(argument)


def _frozen(argument: object, conversions: set[str]) -> object:
    if isinstance(argument, str) and type(argument) is str:
        scrubbed = scrub_credentials(argument)
        return argument if scrubbed == argument else scrubbed
    if _is_plain(argument):
        return argument
    if type(argument) in (list, tuple) and all(_is_plain(item) for item in argument):
        return list(argument) if type(argument) is list else argument
    number = _number(argument, conversions - set(_TEXT_CONVERSIONS))
    texts = conversions & set(_TEXT_CONVERSIONS)
    if number is not None and not texts:
        return number
    shown = "r" if "r" in texts else "a" if "a" in texts else None
    text_conversion = "s" if "s" in texts or shown is None else shown
    text = scrub_credentials(_rendered(argument, _TEXT_CONVERSIONS[text_conversion]))
    if shown == text_conversion:
        shown_text = text
    elif shown is not None:
        shown_text = scrub_credentials(_rendered(argument, _TEXT_CONVERSIONS[shown]))
    else:
        shown_text = None
    if isinstance(number, int):
        return _FrozenInt(number, text, shown_text)
    if isinstance(number, float):
        return _FrozenFloat(number, text, shown_text)
    return _FrozenText(text, shown_text)


def _number(argument: object, numeric: set[str]) -> int | float | None:
    if len(numeric) != 1:
        return None
    conversion = next(iter(numeric))
    try:
        if conversion in _INDEX_CONVERSIONS:
            return operator.index(argument)
        if conversion in _INT_CONVERSIONS:
            return int(argument)
        if conversion in _FLOAT_CONVERSIONS:
            return float(argument)
    except Exception:
        return None
    return None


def _directives(message: str) -> list[tuple[str | None, str]]:
    found = []
    for directive in _DIRECTIVE.finditer(message):
        conversion = directive["conversion"]
        if conversion == "%":
            continue
        if directive["width"] == "*":
            found.append((None, "*"))
        if directive["precision"] == "*":
            found.append((None, "*"))
        found.append((directive["key"], conversion))
    return found


def _frozen_arguments(message: str, arguments: object) -> tuple | dict:
    directives = _directives(message)
    if isinstance(arguments, Mapping) and (not directives or any(key is not None for key, _ in directives)):
        conversions: dict = {}
        for key, conversion in directives:
            conversions.setdefault(key, set()).add(conversion)
        return {key: _frozen(value, conversions.get(key, {"s"})) for key, value in arguments.items()}
    positional = arguments if isinstance(arguments, tuple) else (arguments,)
    if len(directives) != len(positional):
        return tuple(_frozen(argument, {"r"}) for argument in positional)
    return tuple(_frozen(argument, {conversion}) for argument, (_, conversion) in zip(positional, directives))


def _text_of(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return object.__repr__(value)


def _scrub_message(record: logging.LogRecord) -> None:
    message = _text_of(record.msg)
    if not record.args:
        scrubbed = scrub_credentials(message)
        record.msg = record.msg if scrubbed == message and isinstance(record.msg, str) else scrubbed
        return
    try:
        message % record.args
    except Exception:
        record.msg, record.args = scrub_credentials(f"{message} {_rendered(record.args, repr)}"), None
        return
    arguments = _frozen_arguments(message, record.args)
    try:
        rendered = message % arguments
    except Exception:
        record.msg, record.args = scrub_credentials(f"{message} {_rendered(arguments, repr)}"), None
        return
    scrubbed = scrub_credentials(rendered)
    if scrubbed == rendered:
        record.msg, record.args = message, arguments
    elif isinstance(arguments, dict):
        masked = {key: _masked_unless_shown(value, scrubbed) for key, value in arguments.items()}
        record.msg, record.args = scrubbed.replace("%", "%%"), masked
    else:
        masked = tuple(_masked_unless_shown(argument, scrubbed) for argument in arguments)
        record.msg, record.args = scrubbed.replace("%", "%%") + "%.0s" * len(masked), masked


def _masked_unless_shown(argument: object, shown: str) -> object:
    if isinstance(argument, str) and str(argument) not in shown:
        return _FrozenText("***")
    return argument


def _already_scrubbed(record: logging.LogRecord) -> bool:
    state = record.__dict__.get(_SCRUBBED_STATE)
    return state is not None and all(
        kept is current for kept, current in zip(state, (record.msg, record.args, record.exc_text, record.stack_info))
    )


def _scrub_record(record: logging.LogRecord) -> None:
    _scrub_message(record)
    if record.exc_info and not record.exc_text:
        record.exc_text = _FORMAT_EXCEPTION(_EXCEPTION_FORMATTER, record.exc_info)
    if record.exc_text:
        record.exc_text = scrub_credentials(record.exc_text)
    if record.stack_info:
        record.stack_info = scrub_credentials(record.stack_info)
    record.__dict__[_SCRUBBED_STATE] = (record.msg, record.args, record.exc_text, record.stack_info)


class CredentialScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _already_scrubbed(record):
            _scrub_record(record)
        return True


class _ScrubbingRecordFactory:
    def __init__(self, inner) -> None:
        self.inner = inner

    def __call__(self, *args, **kwargs) -> logging.LogRecord:
        record = self.inner(*args, **kwargs)
        if not _already_scrubbed(record):
            _scrub_record(record)
        return record


def _scrubbed_format_exception(self: logging.Formatter, exc_info) -> str:
    return scrub_credentials(_FORMAT_EXCEPTION(self, exc_info))


def _scrub_at_record_creation() -> None:
    factory = logging.getLogRecordFactory()
    if not isinstance(factory, _ScrubbingRecordFactory):
        logging.setLogRecordFactory(_ScrubbingRecordFactory(factory))
    if logging.Formatter.formatException is _FORMAT_EXCEPTION:
        logging.Formatter.formatException = _scrubbed_format_exception


def _reaches_no_other_handler(record: logging.LogRecord) -> bool:
    if any(not isinstance(handler, ScrubbedRootHandler) for handler in logging.root.handlers):
        return False
    logger = logging.root.manager.loggerDict.get(record.name)
    while isinstance(logger, logging.Logger) and logger is not logging.root:
        if logger.handlers:
            return False
        if not logger.propagate:
            return False
        logger = logger.parent
    return True


class ScrubbedRootHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.addFilter(_reaches_no_other_handler)
        self.addFilter(CredentialScrubFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


def _filter_handler(handler: logging.Handler) -> None:
    if not any(isinstance(existing, CredentialScrubFilter) for existing in handler.filters):
        handler.addFilter(CredentialScrubFilter())


def scrub_credentials_at_every_handler() -> None:
    _scrub_at_record_creation()
    if not any(isinstance(handler, ScrubbedRootHandler) for handler in logging.root.handlers):
        logging.root.addHandler(ScrubbedRootHandler())
    for handler in logging.root.handlers:
        _filter_handler(handler)
    if logging.lastResort is not None:
        _filter_handler(logging.lastResort)
    for logger in list(logging.root.manager.loggerDict.values()):
        if isinstance(logger, logging.Logger):
            for handler in logger.handlers:
                _filter_handler(handler)


def scrub_credentials_in_config(config: dict) -> None:
    config.setdefault("filters", {})["scrub_credentials"] = {"()": CredentialScrubFilter}
    for handler in config.get("handlers", {}).values():
        filters = handler.setdefault("filters", [])
        if "scrub_credentials" not in filters:
            filters.append("scrub_credentials")
