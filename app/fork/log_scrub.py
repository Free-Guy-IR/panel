import logging
import sys
from collections.abc import MutableMapping

from app.fork.operation.node_messages import scrub_credentials

_EXCEPTION_FORMATTER = logging.Formatter()
_PLAIN_ARGUMENT = (str, int, float, bool, type(None))


def _scrub_argument(argument: object) -> object:
    if isinstance(argument, str):
        return scrub_credentials(argument)
    if isinstance(argument, _PLAIN_ARGUMENT):
        return argument
    return scrub_credentials(argument)


def _message_of(record: logging.LogRecord) -> str | None:
    try:
        return record.getMessage()
    except Exception:
        return None


class CredentialScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_scrub_argument(argument) for argument in record.args)
        elif isinstance(record.args, MutableMapping) and record.args:
            record.args = {key: _scrub_argument(value) for key, value in record.args.items()}
        message = _message_of(record)
        if message is None:
            record.msg, record.args = scrub_credentials(record.msg), None
        elif scrub_credentials(message) != message:
            record.msg, record.args = scrub_credentials(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = _EXCEPTION_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub_credentials(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub_credentials(record.stack_info)
        return True


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
            sys.stderr.write(scrub_credentials(self.format(record)) + "\n")
            sys.stderr.flush()
        except Exception:
            self.handleError(record)


def _filter_handler(handler: logging.Handler) -> None:
    if not any(isinstance(existing, CredentialScrubFilter) for existing in handler.filters):
        handler.addFilter(CredentialScrubFilter())


def scrub_credentials_at_every_handler() -> None:
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
