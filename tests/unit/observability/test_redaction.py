from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from agent_hub.observability.logging import (
    REDACTED,
    LogLevel,
    configure_logging,
    get_secure_logger,
    redact,
)
from agent_hub.observability.metrics import default_metrics_registry
from agent_hub.observability.tracing import current_trace_id, set_trace_id


def test_structured_log_redacts_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    secure_logger = get_secure_logger("agent_hub.test", component="runtime")

    secure_logger.info(
        "provider failed",
        api_key="sk-secret",
        authorization="Bearer abc",
        nested={"password": "secret-password"},
    )

    output = caplog.text
    assert "sk-secret" not in output
    assert "Bearer abc" not in output
    assert "secret-password" not in output
    assert REDACTED in output
    assert '"component":"runtime"' in output


def test_configured_system_logging_outputs_json_with_levels_and_filters_debug() -> None:
    stream = StringIO()
    configure_logging(level=LogLevel.WARNING, stream=stream)
    logger = get_secure_logger("agent_hub.test.system", component="worker", run_id="run-1")

    logger.debug("debug event", token="sk-hidden")
    logger.warning("warning event", token="sk-hidden")
    logger.critical("critical event", error_type="FatalError")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert [line["level"] for line in lines] == ["WARNING", "CRITICAL"]
    assert lines[0]["event"] == "warning event"
    assert lines[0]["component"] == "worker"
    assert lines[0]["run_id"] == "run-1"
    assert lines[0]["token"] == REDACTED
    assert lines[1]["error_type"] == "FatalError"


def test_default_system_logging_level_is_warning_to_avoid_log_volume() -> None:
    stream = StringIO()
    configure_logging(stream=stream)
    logger = get_secure_logger("agent_hub.test.default", component="worker")

    logger.info("routine progress")
    logger.warning("operator-visible warning")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert [line["level"] for line in lines] == ["WARNING"]
    assert lines[0]["event"] == "operator-visible warning"


def test_configured_plain_python_logging_is_json_and_redacted() -> None:
    stream = StringIO()
    configure_logging(level="INFO", stream=stream)

    logging.getLogger("agent_hub.worker").error("provider failed with sk-secret")

    payload = json.loads(stream.getvalue())

    assert payload["level"] == "ERROR"
    assert payload["logger"] == "agent_hub.worker"
    assert payload["message"] == f"provider failed with {REDACTED}"


def test_redaction_preserves_safe_fields() -> None:
    assert redact({"run_id": "run-1", "cost": 1.25}) == {"run_id": "run-1", "cost": 1.25}


def test_metrics_render_prometheus_without_unbounded_labels() -> None:
    registry = default_metrics_registry()
    registry.counter("agent_hub_runs_total", "Total runs accepted by status.").inc()
    registry.gauge("agent_hub_queue_depth", "Current run queue depth.").set(3)

    rendered = registry.render_prometheus()

    assert "# TYPE agent_hub_runs_total counter" in rendered
    assert "agent_hub_runs_total 1" in rendered
    assert "agent_hub_queue_depth 3" in rendered
    assert "tenant_id" not in rendered


def test_trace_id_can_be_bound_and_reused() -> None:
    set_trace_id("trace-1")

    assert current_trace_id() == "trace-1"
