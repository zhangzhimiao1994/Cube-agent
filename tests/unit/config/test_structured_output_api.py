from copy import deepcopy
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.config.repository import ConfigRepository, ConfigRevision, ConfigStatus
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import ConfigService, _validated_document
from agent_hub.models.types import Deployment
from agent_hub.runtime.defaults import _deployments
from tests.unit.config.test_service import TransactionStub


def document(protocol: str | None = None) -> dict[str, object]:
    deployment: dict[str, object] = {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "api_base": "https://provider.example/v1",
        "credential_ref": "secret-reference",
        "quota_scope_id": "account",
        "capabilities": ["text", "structured_output"],
        "input_per_million_usd": "0.1",
        "output_per_million_usd": "0.2",
    }
    if protocol is not None:
        deployment["structured_output_api"] = protocol
    return {"models": {"main": {"deployments": [deployment]}}, "agents": []}


def test_legacy_document_and_deployment_default_to_chat() -> None:
    config = PlatformConfig.model_validate(document())
    assert config.models["main"].deployments[0].structured_output_api == "chat_completions"
    assert Deployment(id="main_1", logical_model="main").structured_output_api == "chat_completions"
    assert _deployments(config)[0].structured_output_api == "chat_completions"


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_structured_output_api_round_trip_and_runtime_construction(protocol: str) -> None:
    original = document(protocol)
    before = deepcopy(original)
    config = PlatformConfig.model_validate(original)
    restored = PlatformConfig.model_validate_json(config.model_dump_json())
    deployment = _deployments(restored)[0]
    assert deployment.structured_output_api == protocol
    assert deployment.api_base == "https://provider.example/v1"
    assert deployment.secret_ref == "secret-reference"
    assert deployment.request_model == "deepseek-flash"
    assert str(deployment.input_per_million_usd) == "0.1"
    assert original == before
    dumped = restored.model_dump(mode="json")["models"]["main"]["deployments"][0]
    assert dumped.get("structured_output_api") == (protocol if protocol == "responses" else None)


@pytest.mark.parametrize("invalid", ["auto", "json_object", "RESPONSES", "responses ", None])
def test_invalid_structured_output_api_is_rejected(invalid: object) -> None:
    with pytest.raises(ValueError, match="structured_output_api"):
        Deployment(id="main_1", logical_model="main", structured_output_api=invalid)  # type: ignore[arg-type]
    raw = document()
    raw["models"]["main"]["deployments"][0]["structured_output_api"] = invalid  # type: ignore[index]
    with pytest.raises(ValidationError, match="structured_output_api"):
        PlatformConfig.model_validate(raw)


@pytest.mark.parametrize("protocol", [None, "chat_completions", "responses"])
async def test_rollback_persists_only_nondefault_structured_output_api(
    protocol: str | None,
) -> None:
    tenant, actor = uuid4(), uuid4()
    original = document(protocol)
    before = deepcopy(original)
    source = MagicMock(status=ConfigStatus.SUPERSEDED.value, document=original)
    repository = MagicMock(spec=ConfigRepository)
    repository.lock_tenant = AsyncMock()
    repository.get_version = AsyncMock(return_value=source)
    repository.get_current_row = AsyncMock(return_value=None)
    repository.next_version = AsyncMock(return_value=3)

    async def create(_session: object, **values: object) -> ConfigRevision:
        return ConfigRevision(
            id=uuid4(),
            tenant_id=tenant,
            version=3,
            status=ConfigStatus.PUBLISHED,
            document=cast(dict[str, object], values["document"]),
            created_by=actor,
            created_at=datetime.now(UTC),
        )

    repository.create = AsyncMock(side_effect=create)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.begin.return_value = TransactionStub()
    service = ConfigService(
        cast(async_sessionmaker[AsyncSession], MagicMock(return_value=session)),
        repository,
    )
    restored = await service.rollback(tenant, 1, actor)
    deployed = restored.document["models"]["main"]["deployments"][0]  # type: ignore[index]
    if protocol == "responses":
        assert deployed["structured_output_api"] == "responses"
    else:
        assert "structured_output_api" not in deployed
    assert _deployments(PlatformConfig.model_validate(restored.document))[
        0
    ].structured_output_api == (protocol or "chat_completions")
    assert restored.document == _validated_document(original, include_defaults=True)
    assert original == before
