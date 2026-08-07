from __future__ import annotations

from fastapi.testclient import TestClient

from agent_hub.app import create_app


class Probe:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def __call__(self) -> None:
        if self.fail:
            raise RuntimeError("internal dependency address")


def test_readiness_reports_safe_failed_dependency_status() -> None:
    app = create_app(database_probe=Probe(), redis_probe=Probe(fail=True))

    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "redis": "failed"}
    assert "internal dependency address" not in response.text


def test_readiness_runs_extra_production_checks() -> None:
    app = create_app(database_probe=Probe(), redis_probe=Probe())

    async def migration_check() -> None:
        return None

    app.state.extra_readiness_checks = {"migration": migration_check}
    response = TestClient(app).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_endpoint_exports_prometheus_text() -> None:
    app = create_app(database_probe=Probe(), redis_probe=Probe())
    app.state.metrics_registry.counter(
        "agent_hub_runs_total",
        "Total runs accepted by status.",
    ).inc(2)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "agent_hub_runs_total 2" in response.text
