import pytest
from pydantic import ValidationError

from app.models.proxy import L2TP_PASSWORD_MAX_LENGTH, L2TP_PASSWORD_MIN_LENGTH, L2TPSettings, ProxyTable
from app.utils.l2tp import generate_l2tp_password


@pytest.mark.parametrize(
    "password",
    [
        "short",
        "a" * (L2TP_PASSWORD_MAX_LENGTH + 1),
        "has space here",
        "line\nroot ALL * *",
        "tab\there\tvalue",
        "carriage\rreturn",
        "nul\x00byte",
    ],
)
def test_rejects_passwords_that_could_break_chap_secrets(password):
    with pytest.raises(ValidationError):
        L2TPSettings(password=password)


def test_rejects_non_string_password():
    with pytest.raises(ValidationError):
        L2TPSettings(password=b"bytespassword12345678")


def test_accepts_generated_password_and_none():
    generated = generate_l2tp_password()
    assert L2TPSettings(password=generated).password == generated
    assert L2TPSettings(password=None).password is None


def test_accepts_printable_ascii_at_both_length_bounds():
    assert L2TPSettings(password="a" * L2TP_PASSWORD_MIN_LENGTH).password
    assert L2TPSettings(password="a" * L2TP_PASSWORD_MAX_LENGTH).password
    assert L2TPSettings(password="p@ss-w0rd!#$%^&*()_+").password


def test_proxy_table_rejects_an_injecting_password():
    with pytest.raises(ValidationError):
        ProxyTable.model_validate({"l2tp": {"password": 'x" *\nattacker l2tp-de "y'}})


def test_empty_password_clears_the_field_instead_of_raising():
    assert L2TPSettings(password="").password is None
    assert ProxyTable.model_validate({"l2tp": {"password": ""}}).l2tp.password is None


def test_stored_password_is_revalidated_before_it_reaches_a_node():
    from app.node.user import safe_l2tp_password

    good = "Ab3xyzAb3xyzAb3xyzAb"
    assert safe_l2tp_password(good, 7) == good
    assert safe_l2tp_password('x\nattacker l2tp-de "pw" *', 7) is None
    assert safe_l2tp_password("", 7) is None
    assert safe_l2tp_password(None, 7) is None
    assert safe_l2tp_password("a" * 65, 7) is None
