from __future__ import annotations

import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from agent_hub.api.routers.admin import (
    PluginCapabilityRequest,
    PluginResourceRequest,
    PluginResourceResponse,
)
from agent_hub.plugins.contracts import plugin_package_stdin_schema
from agent_hub.plugins.runtime import PluginInvocationContext, _plugin_package_runner_request


@pytest.fixture
def sdk_request() -> dict[str, Any]:
    plugin = PluginResourceResponse(
        **PluginResourceRequest(
            id="text-statistics", name="Text Statistics", version="1.0.0",
            resource_config={"zone": "utc", "nested": {"enabled": True}},
        ).model_dump(),
        status="running", health="healthy",
    )
    capability = PluginCapabilityRequest(
        id="text.statistics", capability_config={"mode": "count"},
    )
    payload = _plugin_package_runner_request(
        plugin=plugin, capability=capability, arguments={"text": "Cube Agent"},
        context=PluginInvocationContext(
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            user_id=UUID("00000000-0000-4000-8000-000000000002"),
            run_id=UUID("00000000-0000-4000-8000-000000000003"),
            actor="tester", idempotency_key="statistics-1",
        ),
    )
    return cast(dict[str, Any], json.loads(json.dumps(payload)))


def _validator() -> Draft202012Validator:
    schema = json.loads(json.dumps(plugin_package_stdin_schema()))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_published_stdin_schema_matches_real_sdk_with_nonempty_configs(
    sdk_request: dict[str, Any],
) -> None:
    assert sdk_request == {
        "schema_version": 1,
        "plugin_id": "text-statistics",
        "capability_id": "text.statistics",
        "arguments": {"text": "Cube Agent"},
        "resource_config": {"zone": "utc", "nested": {"enabled": True}},
        "capability_config": {"mode": "count"},
        "context": {
            "tenant_id": "00000000-0000-4000-8000-000000000001",
            "user_id": "00000000-0000-4000-8000-000000000002",
            "run_id": "00000000-0000-4000-8000-000000000003",
            "actor": "tester", "idempotency_key": "statistics-1",
        },
    }
    _validator().validate(sdk_request)
    sdk_request["future_metadata"] = {"trace": "optional"}
    sdk_request["context"]["future_context"] = True
    _validator().validate(sdk_request)


@pytest.mark.parametrize("field", (
    "schema_version", "plugin_id", "capability_id", "arguments",
    "resource_config", "capability_config", "context",
))
def test_published_stdin_schema_requires_each_sdk_field(
    sdk_request: dict[str, Any], field: str,
) -> None:
    del sdk_request[field]
    assert not _validator().is_valid(sdk_request)


@pytest.mark.parametrize(("field", "value"), (
    ("schema_version", True), ("schema_version", 2),
    ("arguments", []), ("resource_config", None), ("capability_config", "count"),
    ("context", {"actor": "tester"}),
))
def test_published_stdin_schema_rejects_invalid_envelopes(
    sdk_request: dict[str, Any], field: str, value: object,
) -> None:
    sdk_request[field] = value
    assert not _validator().is_valid(sdk_request)


@pytest.mark.parametrize("extra_business_argument", (False, True))
def test_real_example_consumes_full_sdk_pipes_without_business_wrapper(
    sdk_request: dict[str, Any], extra_business_argument: bool,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    entrypoint = Path(__file__).parents[3] / "examples/plugins/text-statistics/adapter/main.py"
    adapter = runpy.run_path(str(entrypoint))
    sdk_request["future_metadata"] = {"trace": "optional"}
    sdk_request["context"]["future_context"] = True
    if extra_business_argument:
        sdk_request["arguments"]["unexpected"] = True
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(sdk_request)))
    exit_code = adapter["main"]()
    captured = capsys.readouterr()
    if extra_business_argument:
        assert exit_code == 2
        assert captured.out == ""
        assert "invalid arguments" in captured.err
    else:
        assert exit_code == 0
        assert captured.err == ""
        assert json.loads(captured.out) == {
            "ok": True, "character_count": 10, "non_whitespace_count": 9,
            "line_count": 1, "word_count": 2, "chinese_character_count": 0,
            "utf8_byte_count": 10,
        }
