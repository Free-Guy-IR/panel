import enum
import logging
import sys
from decimal import Decimal

import pytest

from app.fork.log_scrub import CredentialScrubFilter

SECRET = "Zq" + str(7) + "KpWd"
URL = "http://u:" + SECRET + "@h:1"
BOT = str(123456789) + ":" + "AAH" + "dqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"


class _Level(enum.IntEnum):
    HIGH = 7


class _Indexable:
    def __index__(self) -> int:
        return 42

    def __float__(self) -> float:
        return 4.5

    def __repr__(self) -> str:
        return "Indexable()"


class _Plain:
    def __str__(self) -> str:
        return "plain-str"

    def __repr__(self) -> str:
        return "plain-repr"


class _Leaky:
    def __str__(self) -> str:
        return "leaky " + URL

    def __repr__(self) -> str:
        return "Leaky(" + URL + ")"


VALUES = [
    [1, 2, 3],
    (4, "five"),
    {"a": 1, "b": [2]},
    {7},
    frozenset({"x"}),
    Decimal("12.75"),
    _Level.HIGH,
    b"raw-bytes",
    ValueError("bad", 3),
    _Plain(),
    _Indexable(),
    True,
    3.25,
    None,
]
FORMATS = ["%s", "%r", "%d", "%f", "%x", "value=%s tail", "%-8s|"]
MAPPING_FORMATS = ["%(k)s", "%(k)r", "%(k)d", "%(k)f", "%(k)x"]


def _record(message, args, exc_info=None, stack_info=None) -> logging.LogRecord:
    record = logging.LogRecord("arg-types", logging.WARNING, __file__, 1, message, args, exc_info)
    record.stack_info = stack_info
    return record


def _raw_message(message, args) -> str | None:
    try:
        return _record(message, args).getMessage()
    except Exception:
        return None


def _scrubbed(record: logging.LogRecord) -> logging.LogRecord:
    assert CredentialScrubFilter().filter(record)
    return record


def _cases():
    for value in VALUES:
        for message in FORMATS:
            yield message, (value,)
        for message in MAPPING_FORMATS:
            yield message, ({"k": value},)


@pytest.mark.parametrize(("message", "args"), list(_cases()), ids=repr)
def test_a_message_without_a_secret_is_byte_identical_and_keeps_its_arguments(message, args):
    raw = _raw_message(message, args)
    if raw is None:
        pytest.skip("plain logging cannot format this pair either")
    record = _record(message, args)
    kept = record.args
    _scrubbed(record)
    assert record.getMessage() == raw
    assert record.args is kept


def test_numeric_conversions_of_non_builtin_numbers_still_format():
    pairs = [
        ("%d", (Decimal("12.75"),)),
        ("%f", (Decimal("12.75"),)),
        ("%d", (_Level.HIGH,)),
        ("%x", (_Level.HIGH,)),
        ("%d", (_Indexable(),)),
        ("%x", (_Indexable(),)),
        ("%f", (_Indexable(),)),
        ("%(k)d", ({"k": Decimal(3)},)),
    ]
    for message, args in pairs:
        raw = _raw_message(message, args)
        assert raw is not None
        assert _scrubbed(_record(message, args)).getMessage() == raw


def test_a_list_argument_stays_a_list():
    sample = list(range(20))
    record = _scrubbed(_record("sample %s of %d", (sample, 60)))
    assert record.args[0] is sample
    assert record.getMessage() == f"sample {sample} of 60"


SECRET_VALUES = [
    [URL],
    (URL,),
    {"url": URL},
    {URL},
    [[{"inner": (URL,)}]],
    URL.encode(),
    ValueError(URL),
    RuntimeError("context", URL),
    _Leaky(),
    {"password": SECRET},
    [BOT],
]


@pytest.mark.parametrize("value", SECRET_VALUES, ids=lambda value: type(value).__name__)
@pytest.mark.parametrize("message", ["%s", "%r", "%(k)s", "%(k)r"])
def test_a_secret_inside_any_argument_is_scrubbed_from_the_final_message(message, value):
    args = ({"k": value},) if "(k)" in message else (value,)
    record = _scrubbed(_record(message, args))
    rendered = logging.Formatter("%(message)s").format(record)
    assert SECRET not in rendered
    assert BOT.split(":")[1] not in rendered
    assert "***" in rendered


def test_exception_and_stack_text_are_scrubbed():
    try:
        raise ConnectionError("cannot reach " + URL)
    except ConnectionError:
        exc_info = sys.exc_info()
    record = _scrubbed(
        _record("failed %s", ([1],), exc_info=exc_info, stack_info="Stack (most recent call last):\n  " + URL)
    )
    rendered = logging.Formatter("%(message)s").format(record)
    assert SECRET not in rendered
    assert "failed [1]" in rendered
    assert "ConnectionError: cannot reach http://***@h:1" in rendered
    assert "Stack (most recent call last)" in rendered


def test_a_container_changed_after_the_scrub_is_scrubbed_again_by_a_later_handler():
    payload = ["clean"]
    record = _scrubbed(_record("payload %s", (payload,)))
    payload.append(URL)
    _scrubbed(record)
    assert SECRET not in record.getMessage()


def test_an_unformattable_record_keeps_its_arguments_without_their_secrets():
    record = _scrubbed(_record("count %d of %s", ("many", URL)))
    with pytest.raises(TypeError):
        record.getMessage()
    assert SECRET not in repr(record.msg) + repr(record.args)
    assert "many" in repr(record.args)
