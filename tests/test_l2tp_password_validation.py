import pytest
from pydantic import ValidationError

from app.models.proxy import (
    L2TP_PASSWORD_MAX_LENGTH,
    L2TP_PASSWORD_MIN_LENGTH,
    L2TPSettings,
    ProxyTable,
    ProxyTableInput,
    StrictL2TPSettings,
)
from app.utils.l2tp import generate_l2tp_password

UNSAFE = [
    "short",
    "a" * (L2TP_PASSWORD_MAX_LENGTH + 1),
    "has space here",
    "line\nroot ALL * *",
    "tab\there\tvalue",
    "carriage\rreturn",
    "nul\x00byte",
]


@pytest.mark.parametrize("password", UNSAFE)
def test_input_rejects_passwords_that_could_break_chap_secrets(password):
    with pytest.raises(ValidationError):
        StrictL2TPSettings(password=password)
    with pytest.raises(ValidationError):
        ProxyTableInput.model_validate({"l2tp": {"password": password}})


@pytest.mark.parametrize("password", UNSAFE)
def test_stored_data_discards_the_same_passwords_instead_of_raising(password):
    assert L2TPSettings(password=password).password is None
    assert ProxyTable.model_validate({"l2tp": {"password": password}}).l2tp.password is None


def test_input_rejects_a_non_string_password():
    with pytest.raises(ValidationError):
        StrictL2TPSettings(password=b"bytespassword12345678")


def test_stored_non_string_password_is_discarded():
    assert L2TPSettings(password=b"bytespassword12345678").password is None


def test_accepts_generated_password_and_none_on_both_models():
    generated = generate_l2tp_password()
    for model in (L2TPSettings, StrictL2TPSettings):
        assert model(password=generated).password == generated
        assert model(password=None).password is None


def test_accepts_printable_ascii_at_both_length_bounds():
    assert StrictL2TPSettings(password="a" * L2TP_PASSWORD_MIN_LENGTH).password
    assert StrictL2TPSettings(password="a" * L2TP_PASSWORD_MAX_LENGTH).password
    assert StrictL2TPSettings(password="p@ss-w0rd!#$%^&*()_+").password


def test_input_rejects_an_injecting_password_while_storage_discards_it():
    payload = {"l2tp": {"password": 'x" *\nattacker l2tp-de "y'}}
    with pytest.raises(ValidationError):
        ProxyTableInput.model_validate(payload)
    assert ProxyTable.model_validate(payload).l2tp.password is None


def test_empty_password_clears_the_field_on_both_models():
    assert L2TPSettings(password="").password is None
    assert StrictL2TPSettings(password="").password is None
    assert ProxyTable.model_validate({"l2tp": {"password": ""}}).l2tp.password is None
    assert ProxyTableInput.model_validate({"l2tp": {"password": ""}}).l2tp.password is None


def test_stored_password_is_revalidated_before_it_reaches_a_node():
    from app.node.user import safe_l2tp_password

    good = "Ab3xyzAb3xyzAb3xyzAb"
    assert safe_l2tp_password(good, 7) == good
    assert safe_l2tp_password('x\nattacker l2tp-de "pw" *', 7) is None
    assert safe_l2tp_password("", 7) is None
    assert safe_l2tp_password(None, 7) is None
    assert safe_l2tp_password("a" * 65, 7) is None
