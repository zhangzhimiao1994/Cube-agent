"""Local opt-in/DSN boundary checks only; these do not prove live Redis or PG."""
from types import SimpleNamespace

import pytest

from tests.integration.models import capacity_native_redis_fixture as native

DSN = "postgresql+asyncpg://agent_hub_test@/agent_hub_test?host=/run/mofang-review-pg-" + "a" * 32 + "/socket"


def environment() -> dict[str, str]:
    return {
        "AGENT_HUB_NATIVE_OWNED_UNIX_REDIS": "1",
        "AGENT_HUB_HANDOVER_TEST_DB_ISOLATED": "1",
        "AGENT_HUB_DATABASE_URL": DSN,
        "AGENT_HUB_TEST_DATABASE_URL": DSN,
    }


def test_exact_private_native_environment_is_recognized_without_IO(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "os", SimpleNamespace(name="posix", environ=environment()))
    native.require_native_pg_environment()


@pytest.mark.parametrize("invalid", [
    "missing_owned_flag", "missing_sql_optin", "different_urls", "tcp", "user", "database",
    "password", "foreign_socket", "parent_path", "extra_query", "malformed", "platform",
])
async def test_invalid_native_environment_fails_before_redis_discovery(
    monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    values = environment()
    system = "posix"
    if invalid == "missing_owned_flag":
        values.pop("AGENT_HUB_NATIVE_OWNED_UNIX_REDIS")
    elif invalid == "missing_sql_optin":
        values.pop("AGENT_HUB_HANDOVER_TEST_DB_ISOLATED")
    elif invalid == "different_urls":
        values["AGENT_HUB_DATABASE_URL"] = DSN + "different"
    elif invalid == "platform":
        system = "nt"
    else:
        replacements = {
            "tcp": "postgresql+asyncpg://agent_hub_test@127.0.0.1:5432/agent_hub_test",
            "user": DSN.replace("agent_hub_test@", "agent_hub@"),
            "database": DSN.replace("/agent_hub_test?", "/agent_hub?"),
            "password": DSN.replace("agent_hub_test@", "agent_hub_test:fake@"),
            "foreign_socket": DSN.replace("/run/mofang-review-pg-" + "a" * 32 + "/socket", "/run/postgresql"),
            "parent_path": DSN + "/../other",
            "extra_query": DSN + "&sslmode=prefer",
            "malformed": "invalid-url",
        }
        values["AGENT_HUB_TEST_DATABASE_URL"] = values["AGENT_HUB_DATABASE_URL"] = replacements[invalid]
    monkeypatch.setattr(native, "os", SimpleNamespace(name=system, environ=values))

    def forbidden_discovery(name: str) -> str:
        raise AssertionError("invalid native scope must not start or discover Redis")

    monkeypatch.setattr(native, "shutil", SimpleNamespace(which=forbidden_discovery))
    with pytest.raises(ValueError, match="verified private PG test environment"):
        async with native.owned_native_redis():
            raise AssertionError("invalid native scope must not enter the Redis fixture")
