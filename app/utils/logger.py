import logging
from copy import copy

import click
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import DefaultFormatter


class CustomLoggingFormatter(DefaultFormatter):
    def formatMessage(self, record: logging.LogRecord) -> str:
        recordcopy = copy(record)
        recordcopy.__dict__["nameprefix"] = click.style(record.name.capitalize(), fg="blue")
        return super().formatMessage(recordcopy)


LOGGING_CONFIG["formatters"]["custom"] = {
    "()": CustomLoggingFormatter,
    "fmt": "%(levelprefix)s %(nameprefix)s - %(message)s",
    "use_colors": None,
}
LOGGING_CONFIG["handlers"]["custom"] = {
    "class": LOGGING_CONFIG["handlers"]["default"]["class"],
    "formatter": "custom",
    "stream": LOGGING_CONFIG["handlers"]["default"]["stream"],
}


def get_logger(name: str = "uvicorn.error") -> logging.Logger:
    if not LOGGING_CONFIG["loggers"].get(name):
        LOGGING_CONFIG["loggers"][name] = {
            "handlers": ["custom"],
            "level": LOGGING_CONFIG["loggers"]["uvicorn"]["level"],
        }
        logging.config.dictConfig(LOGGING_CONFIG)

    logger = logging.getLogger(name)
    return logger
