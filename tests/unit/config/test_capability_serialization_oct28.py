import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from itertools import permutations
from pathlib import Path
from uuid import UUID

import pytest

from agent_hub.api.schemas import ConfigRevisionResponse
from agent_hub.config.repository import ConfigRevision, ConfigStatus
from agent_hub.config.schema import DeploymentDefinition
from agent_hub.models.types import ModelCapability

CAPABILITIES = [
    "audio",
    "audio_generation",
    "image_generation",
    "structured_output",
    "text",
    "tool_calling",
    "video_generation",
    "vision",
]


def _deployment_values(capabilities: list[str]) -> dict[str, object]:
    return {
        "provider": "example",
        "model": "example-model",
        "api_base": "https://provider.example/v1",
        "credential_ref": "example-credential",
        "quota_scope_id": "example-quota",
        "max_concurrency": 4,
        "target_utilization": 0.75,
        "reserved_slots": 1,
        "rpm": 60,
        "tpm": 6000,
        "context_window_tokens": 8192,
        "max_output_tokens": 1024,
        "input_per_million_usd": "0.125",
        "output_per_million_usd": "0.250",
        "capabilities": capabilities,
    }


def _revision(capabilities: list[str]) -> ConfigRevision:
    return ConfigRevision(
        id=UUID(int=1),
        tenant_id=UUID(int=2),
        version=10,
        status=ConfigStatus.PUBLISHED,
        document={
            "models": {"primary": {"deployments": [_deployment_values(capabilities)]}},
            "agents": [
                {
                    "id": "assistant",
                    "role": "Assistant",
                    "prompt": "Use the configured tools.",
                    "model": "primary",
                    "skills": ["zeta", "alpha"],
                }
            ],
        },
        created_by=UUID(int=3),
        created_at=datetime(2026, 10, 28, tzinfo=UTC),
    )


@pytest.mark.parametrize("order", list(permutations(["text", "vision", "audio", "tool_calling"])))
def test_capability_json_is_stable_for_different_input_orders(order: tuple[str, ...]) -> None:
    definition = DeploymentDefinition.model_validate(_deployment_values(list(order)))
    expected = ["audio", "text", "tool_calling", "vision"]

    assert definition.model_dump(mode="json")["capabilities"] == expected
    assert json.loads(definition.model_dump_json())["capabilities"] == expected


def test_json_round_trip_preserves_capability_set_and_runtime_semantics() -> None:
    definition = DeploymentDefinition.model_validate(
        _deployment_values(list(reversed(CAPABILITIES)) + ["text"])
    )
    before = definition.model_dump()

    serialized = definition.model_dump_json()
    restored = DeploymentDefinition.model_validate_json(serialized)
    runtime = definition.to_deployment(deployment_id="primary-1", logical_model="primary")

    assert json.loads(serialized)["capabilities"] == CAPABILITIES
    assert isinstance(definition.capabilities, set)
    assert isinstance(before["capabilities"], set)
    assert definition.model_dump() == before
    assert restored == definition
    assert isinstance(restored.capabilities, set)
    assert runtime.capabilities == frozenset(ModelCapability(item) for item in CAPABILITIES)


@pytest.mark.parametrize("protocol", [None, "responses"])
def test_capability_serialization_leaves_other_deployment_fields_unchanged(
    protocol: str | None,
) -> None:
    values = _deployment_values(list(reversed(CAPABILITIES)))
    if protocol is not None:
        values["structured_output_api"] = protocol
    before = deepcopy(values)
    definition = DeploymentDefinition.model_validate(values)

    expected_json = {**values, "capabilities": CAPABILITIES}
    assert definition.model_dump(mode="json", by_alias=True) == expected_json
    assert json.loads(definition.model_dump_json(by_alias=True)) == expected_json

    expected_python = {**values, "capabilities": set(CAPABILITIES)}
    expected_python["input_per_million_usd"] = Decimal("0.125")
    expected_python["output_per_million_usd"] = Decimal("0.250")
    assert definition.model_dump(by_alias=True) == expected_python
    assert values == before


def test_revision_response_json_preserves_metadata_and_document_content() -> None:
    revision = _revision(list(reversed(CAPABILITIES)))
    original_document = deepcopy(revision.document)
    response = ConfigRevisionResponse.from_revision(revision)

    expected_document = deepcopy(revision.document)
    expected_document["models"]["primary"]["fallback_model"] = None  # type: ignore[index]
    expected_document["models"]["primary"]["deployments"][0]["capabilities"] = (  # type: ignore[index]
        CAPABILITIES
    )
    expected = {
        "id": str(UUID(int=1)),
        "version": 10,
        "status": "published",
        "document": expected_document,
        "created_by": str(UUID(int=3)),
        "created_at": "2026-10-28T00:00:00Z",
    }
    assert response.model_dump(mode="json", by_alias=True) == expected
    assert json.loads(response.model_dump_json(by_alias=True)) == expected
    assert revision.document == original_document
    assert isinstance(response.document.models["primary"].deployments[0].capabilities, set)


def test_revision_response_json_is_identical_across_process_hash_seeds() -> None:
    script = (
        "import json, runpy, sys; "
        "namespace = runpy.run_path(sys.argv[1]); "
        "revision = namespace['_revision'](json.loads(sys.argv[2])); "
        "response = namespace['ConfigRevisionResponse'].from_revision(revision); "
        "print(response.model_dump_json(by_alias=True))"
    )
    snapshots = []
    for index, seed in enumerate(["0", "1", "2", "7", "42"]):
        order = CAPABILITIES[index:] + CAPABILITIES[:index]
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(Path(__file__).resolve()), json.dumps(order)],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"},
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        snapshots.append(completed.stdout.strip())

    assert len(set(snapshots)) == 1, dict(zip(["0", "1", "2", "7", "42"], snapshots))
    deployment = json.loads(snapshots[0])["document"]["models"]["primary"]["deployments"][0]
    assert deployment["capabilities"] == CAPABILITIES
