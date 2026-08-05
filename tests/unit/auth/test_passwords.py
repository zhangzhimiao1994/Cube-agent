import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from agent_hub.auth.passwords import PasswordService, PasswordValidationError


def test_argon2id_round_trip_and_wrong_password() -> None:
    service = PasswordService()
    password = "correct horse battery staple"

    password_hash = service.hash(password)

    assert password_hash.startswith("$argon2id$")
    assert password not in password_hash
    assert service.verify(password_hash, password) is True
    assert service.verify(password_hash, "incorrect password") is False


def test_malformed_hash_is_the_same_boolean_failure_as_a_wrong_password() -> None:
    service = PasswordService()

    assert service.verify("not-an-argon-hash", "correct horse battery staple") is False


def test_needs_rehash_uses_the_configured_argon2_parameters() -> None:
    old_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    current = PasswordService()
    old_hash = old_hasher.hash("correct horse battery staple")

    assert current.needs_rehash(old_hash) is True
    assert current.needs_rehash(current.hash("correct horse battery staple")) is False
    assert current.needs_rehash("malformed") is False


@pytest.mark.parametrize(
    "password",
    [
        None,
        123,
        "",
        " " * 12,
        "short",
        "x" * 1025,
        "\ud800" + "x" * 11,
    ],
)
def test_password_policy_rejects_invalid_values_without_echoing_them(password: object) -> None:
    service = PasswordService()

    with pytest.raises(PasswordValidationError) as captured:
        service.hash(password)  # type: ignore[arg-type]

    if repr(password) != "''":
        assert repr(password) not in repr(captured.value)
    if str(password):
        assert str(password) not in str(captured.value)


def test_password_policy_accepts_boundaries_without_normalizing() -> None:
    service = PasswordService()

    minimum_hash = service.hash(" x" + "y" * 10)
    maximum_hash = service.hash("z" * 1024)

    assert service.verify(minimum_hash, " x" + "y" * 10)
    assert not service.verify(minimum_hash, "x" + "y" * 10)
    assert service.verify(maximum_hash, "z" * 1024)
