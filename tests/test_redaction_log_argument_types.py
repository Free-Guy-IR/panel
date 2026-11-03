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
    record = _scrubbed(_record(message, args))
    assert record.getMessage() == raw
    assert isinstance(record.args, tuple | dict)


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
    assert type(record.args[0]) is list
    assert record.args[0] == sample
    assert len(record.args[0]) == 20
    sample.append(URL)
    assert record.getMessage() == f"sample {list(range(20))} of 60"


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


def test_an_unformattable_record_is_rendered_once_with_its_arguments_and_no_secret():
    record = _scrubbed(_record("count %d of %s", ("many", URL)))
    assert record.args is None
    assert SECRET not in record.getMessage()
    assert "many" in record.getMessage()
    assert "http://***@h:1" in record.getMessage()


def test_an_unformattable_mapping_record_scrubs_a_password_split_from_its_template():
    record = _scrubbed(_record("%(password)s %(count)d", ({"password": SECRET, "count": "x"},)))
    assert record.args is None
    assert SECRET not in record.getMessage()


def test_a_container_mutated_after_logging_cannot_leak_through_a_formatter_without_the_filter():
    items = ["a"]
    record = _scrubbed(_record("items %s", (items,)))
    items.append(URL)
    rendered = logging.Formatter("%(message)s").format(record)
    assert rendered == "items ['a']"
    assert SECRET not in repr(record.args)


def test_an_object_whose_text_changes_after_logging_keeps_the_text_it_had():
    class _Changing:
        text = "clean"

        def __str__(self) -> str:
            return self.text

    value = _Changing()
    record = _scrubbed(_record("value %s", (value,)))
    value.text = URL
    assert record.getMessage() == "value clean"


@pytest.mark.parametrize(
    ("message", "args"),
    [
        ("%.0s", (URL,)),
        ("%.0s%s", (URL, "shown")),
        ("%(a)s", ({"a": 1, "b": URL},)),
        ("%(a)s", ({"a": 1, "b": [URL]},)),
        ("%.0r", ([URL],)),
    ],
    ids=repr,
)
def test_arguments_the_message_does_not_show_hold_no_secret(message, args):
    record = _scrubbed(_record(message, args))
    assert SECRET not in repr(record.args)
    assert SECRET not in record.getMessage()


def test_a_secret_found_only_in_the_whole_message_is_masked_and_the_arguments_keep_their_shape():
    record = _scrubbed(_record("password: %s status %d", (SECRET, 200)))
    assert SECRET not in record.getMessage()
    assert SECRET not in repr(record.args)
    assert isinstance(record.args, tuple)
    assert len(record.args) == 2
    assert [str(argument) for argument in record.args] == ["***", "***"]
    assert int(record.args[1]) == 0


@pytest.mark.parametrize(
    ("message", "args"),
    [
        ("user %s password=%s%s", (SECRET[:3], SECRET[:3], SECRET[3:])),
        ("user %(a)s password=%(b)s%(c)s", ({"a": SECRET[:3], "b": SECRET[:3], "c": SECRET[3:]},)),
        ("%s %s password=%s", ("x", 7, SECRET)),
    ],
    ids=repr,
)
def test_every_argument_is_masked_when_the_whole_message_holds_a_secret_even_one_shown_elsewhere(message, args):
    raw = _raw_message(message, args)
    record = _scrubbed(_record(message, args))
    values = list(record.args.values()) if isinstance(record.args, dict) else list(record.args)
    assert [str(value) for value in values] == ["***"] * len(values)
    assert [repr(value) for value in values] == ["***"] * len(values)
    assert SECRET[:3] not in repr(record.args)
    assert record.getMessage() == raw.replace(SECRET, "***")
    assert type(record.args) is type(args[0] if isinstance(args[0], dict) else args)


@pytest.mark.parametrize(
    ("message", "args", "expected"),
    [
        ("%(k)d %(k)f", ({"k": Decimal("12.75"), "other": "private-data"},), "12 12.750000"),
        ("%(k)d %(k)x", ({"k": _Indexable(), "other": "private-data"},), "42 2a"),
    ],
    ids=repr,
)
def test_a_record_whose_frozen_arguments_cannot_render_keeps_only_its_first_rendered_text(message, args, expected):
    assert _raw_message(message, args) == expected
    record = _scrubbed(_record(message, args))
    assert record.args is None
    assert record.getMessage() == expected
    assert "private-data" not in record.getMessage()


def test_a_mapping_without_named_directives_stays_a_dict_of_frozen_values():
    items = ["a"]
    record = _scrubbed(_record("hello", ({"items": items, "url": URL},)))
    items.append(URL)
    assert type(record.args) is dict
    assert set(record.args) == {"items", "url"}
    assert record.getMessage() == "hello"
    assert SECRET not in repr(record.args)


def test_the_access_log_tuple_keeps_its_length_and_scrubbed_path():
    args = ("1.2.3.4", "GET", "/x?password=" + SECRET, "1.1", 200)
    record = _scrubbed(_record('%s - "%s %s HTTP/%s" %d', args))
    assert isinstance(record.args, tuple)
    assert len(record.args) == 5
    assert record.args[2] == "/x?password=***"
    assert record.args[4] == 200
    assert record.getMessage() == '1.2.3.4 - "GET /x?password=*** HTTP/1.1" 200'


_ACCESS_SCRIPT = """
import io
import logging
import logging.config
import sys

from app.utils.logger import LOGGING_CONFIG
from app.fork.log_scrub import scrub_credentials_at_every_handler

logging.config.dictConfig(LOGGING_CONFIG)
scrub_credentials_at_every_handler()
SECRET = sys.argv[1]
access = logging.getLogger("uvicorn.access")
out = io.StringIO()
for handler in access.handlers:
    handler.setStream(out)
err = io.StringIO()
sys.stderr = err
for path in ("/api/plain", "/x?password=" + SECRET, "/r?u=http://u:" + SECRET + "@h:1", "/a?pwd:"):
    access.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", path, "1.1", 200, extra={"process_time": "1.00ms"})
sys.stderr = sys.__stderr__
print("ERR=" + repr(err.getvalue()))
print(out.getvalue())
"""


def test_the_uvicorn_access_line_with_a_credential_is_printed_masked_through_the_app_logging_config():
    import os
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    finished = subprocess.run(
        [sys.executable, "-c", _ACCESS_SCRIPT, SECRET],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout
    assert "ERR=''" in printed
    assert SECRET not in printed + finished.stderr
    assert '"GET /api/plain HTTP/1.1" 200' in printed
    assert '"GET /x?password=*** HTTP/1.1" 200' in printed
    assert '"GET /r?u=http://***@h:1 HTTP/1.1" 200' in printed
    assert '*** - "*** *** HTTP/***" 0' in printed
    assert "/a?pwd:" not in printed


_MEMORY_SCRIPT = """
import io
import logging
import logging.handlers
import sys

import app.utils.logger
from app.fork.log_scrub import scrub_credentials_at_every_handler

scrub_credentials_at_every_handler()
URL = sys.argv[1]
out = io.StringIO()
target = logging.StreamHandler(out)
target.setFormatter(logging.Formatter("%(message)s"))
memory = logging.handlers.MemoryHandler(100, flushLevel=logging.CRITICAL, target=target)
logger = logging.getLogger("late-flush")
logger.propagate = False
logger.addHandler(memory)
items = ["a"]
logger.warning("items %s", items)
items.append(URL)
memory.flush()
print(out.getvalue())
"""


def test_a_memory_handler_flushed_after_the_container_changed_prints_the_logged_text():
    import os
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    finished = subprocess.run(
        [sys.executable, "-c", _MEMORY_SCRIPT, URL],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    assert SECRET not in finished.stdout + finished.stderr
    assert "items ['a']" in finished.stdout


@pytest.mark.parametrize(
    ("message", "args"),
    [
        ("%(k)d %(k)s", ({"k": _Level.HIGH},)),
        ("%(k)x %(k)r", ({"k": _Level.HIGH},)),
        ("%(k)f %(k)r", ({"k": Decimal("12.75")},)),
        ("hello", ({"a": 1},)),
        ("100%% done", ({"a": 1},)),
        ("hello", ({"a": URL},)),
    ],
    ids=repr,
)
def test_a_mapping_used_twice_or_not_at_all_renders_like_plain_logging(message, args):
    raw = _raw_message(message, args)
    record = _scrubbed(_record(message, args))
    assert record.getMessage() == raw
    assert SECRET not in repr(record.args)
