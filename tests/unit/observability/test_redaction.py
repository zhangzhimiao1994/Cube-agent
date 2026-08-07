from __future__ import annotations

import logging

import pytest

from agent_hub.observability.logging import REDACTED, get_secure_logger, redact
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
