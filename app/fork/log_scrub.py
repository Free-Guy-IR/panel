import logging
import sys
from collections.abc import Mapping

from app.fork.operation.node_messages import scrub_credentials

_EXCEPTION_FORMATTER = logging.Formatter()
_FORMAT_EXCEPTION = logging.Formatter.formatException
_SCRUBBED_STATE = "_credential_scrub_state"


def _scrubbed_text(argument: object) -> str:
    if isinstance(argument, str):
        return scrub_credentials(argument)
    try:
        return scrub_credentials(repr(argument))
    except Exception:
        return object.__repr__(argument)


def _scrubbed_arguments(arguments: object) -> object:
    if isinstance(arguments, Mapping):
        return {key: _scrubbed_text(value) for key, value in arguments.items()}
    if isinstance(arguments, tuple):
        return tuple(_scrubbed_text(argument) for argument in arguments)
    return _scrubbed_text(arguments)


def _message_of(record: logging.LogRecord) -> str | None:
    try:
        return record.getMessage()
    except Exception:
        return None


def _already_scrubbed(record: logging.LogRecord) -> bool:
    state = record.__dict__.get(_SCRUBBED_STATE)
    if state is None or not all(
        kept is current for kept, current in zip(state, (record.msg, record.args, record.exc_text, record.stack_info))
    ):
        return False
    return not record.args or _message_of(record) == state[4]


def _scrub_record(record: logging.LogRecord) -> None:
    message = _message_of(record)
    if message is None:
        record.msg = scrub_credentials(record.msg)
        if record.args:
            record.args = _scrubbed_arguments(record.args)
    else:
        scrubbed = scrub_credentials(message)
        if scrubbed != message:
            record.msg, record.args, message = scrubbed, None, scrubbed
    if record.exc_info and not record.exc_text:
        record.exc_text = _FORMAT_EXCEPTION(_EXCEPTION_FORMATTER, record.exc_info)
    if record.exc_text:
        record.exc_text = scrub_credentials(record.exc_text)
    if record.stack_info:
        record.stack_info = scrub_credentials(record.stack_info)
    record.__dict__[_SCRUBBED_STATE] = (record.msg, record.args, record.exc_text, record.stack_info, message)


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
