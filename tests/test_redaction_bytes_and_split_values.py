import logging

import pytest

from app.fork.log_scrub import CredentialScrubFilter, _ScrubbingRecordFactory
from app.fork.operation.node_messages import scrub_credentials

SECRET = "Zq" + str(7) + "KpWdXm" + str(42) + "Rt"
SECRET_BYTES = SECRET.encode()
TOKEN_HEAD = "/bot" + str(123456789) + ":A"
TOKEN_TAIL = "AH" + "dqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"


def _through_filter(message, args) -> logging.LogRecord:
    record = logging.LogRecord("bytes-values", logging.WARNING, __file__, 1, message, args, None)
    assert CredentialScrubFilter().filter(record)
    return record


def _through_factory(message, args) -> logging.LogRecord:
    factory = _ScrubbingRecordFactory(logging.LogRecord)
    return factory("bytes-values", logging.WARNING, __file__, 1, message, args, None)


PATHS = pytest.mark.parametrize("scrub", [_through_filter, _through_factory], ids=["filter", "factory"])


@PATHS
@pytest.mark.parametrize(
    ("message", "args"),
    [
        ("password: %r", (SECRET_BYTES,)),
        ("password: %s", (SECRET_BYTES,)),
        ("password=%s", (SECRET_BYTES,)),
        ("password=%r", (SECRET_BYTES,)),
        ("pwd = %r", (SECRET_BYTES,)),
        ("{'password': %r}", (SECRET_BYTES,)),
        ("%r", ({"password": SECRET_BYTES},)),
        ("%s", ({"password": SECRET_BYTES},)),
        ("%s", ({"Password": b"it's " + SECRET_BYTES},)),
        ("%s", ({"passwd": b'it\'s "q" ' + SECRET_BYTES},)),
        ("password: %s", (bytearray(SECRET_BYTES),)),
        ("password=%s", (bytearray(SECRET_BYTES),)),
        ("%r", ({"password": bytearray(SECRET_BYTES)},)),
        ("password: %(p)r", ({"p": SECRET_BYTES},)),
        ("%s", (ValueError("password: " + repr(SECRET_BYTES)),)),
    ],
    ids=repr,
)
def test_a_bytes_password_value_is_masked_whole(scrub, message, args):
    record = scrub(message, args)
    text = record.getMessage()
    assert SECRET not in text
    assert SECRET[1:] not in text
    assert SECRET not in repr(record.args)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password: b'" + SECRET + "'", "password: b'***'"),
        ("password=b'" + SECRET + "'", "password=b'***'"),
        ('password=b"' + SECRET + '"', 'password=b"***"'),
        ("password: bytearray(b'" + SECRET + "')", "password: bytearray(b'***')"),
        ("password=bytearray(b'" + SECRET + "')", "password=bytearray(b'***')"),
        ("{'pwd': b'" + SECRET + "'}", "{'pwd': b'***'}"),
        ("password: b'***'", "password: b'***'"),
        ("password=b'***'", "password=b'***'"),
        ("password: bob", "password: bob"),
        ("password=b", "password=***"),
    ],
    ids=repr,
)
def test_scrub_credentials_masks_a_bytes_literal_password_value(text, expected):
    assert scrub_credentials(text) == expected


@PATHS
def test_a_bot_token_split_across_arguments_is_masked_in_every_argument(scrub):
    record = scrub("%s%s", (TOKEN_HEAD, TOKEN_TAIL))
    text = record.getMessage()
    assert TOKEN_TAIL[:12] not in text
    assert TOKEN_TAIL[:12] not in repr(record.args)
    assert isinstance(record.args, tuple)
    assert len(record.args) == 2
    assert [str(argument) for argument in record.args] == ["***", "***"]


@PATHS
def test_a_bot_token_split_across_mapping_values_is_masked(scrub):
    record = scrub("%(a)s%(b)s", ({"a": TOKEN_HEAD, "b": TOKEN_TAIL},))
    assert TOKEN_TAIL[:12] not in record.getMessage()
    assert TOKEN_TAIL[:12] not in repr(record.args)
    assert type(record.args) is dict


@PATHS
def test_a_password_found_only_in_one_argument_stays_masked_when_the_whole_message_misses_it(scrub):
    record = scrub("x%s tail", ("password=" + SECRET,))
    assert SECRET not in record.getMessage()
    assert SECRET not in repr(record.args)


@PATHS
def test_the_access_log_tuple_with_a_bot_token_path_keeps_five_arguments(scrub):
    args = ("1.2.3.4", "GET", TOKEN_HEAD + TOKEN_TAIL, "1.1", 200)
    record = scrub('%s - "%s %s HTTP/%s" %d', args)
    assert isinstance(record.args, tuple)
    assert len(record.args) == 5
    assert TOKEN_TAIL[:12] not in record.getMessage()
    assert int(record.args[4]) == 200
