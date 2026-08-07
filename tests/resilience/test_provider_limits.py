from __future__ import annotations

from tests.api.test_admin_resources import client, headers


def test_provider_limit_probe_caps_recommendation_below_requested_capacity() -> None:
    response = client().post(
        "/api/v1/admin/models/probe",
        headers=headers(),
        json={"quota_scope": "deepseek_account_1", "desired_concurrency": 128},
    )

    assert response.status_code == 200
    assert response.json()["recommended_concurrency"] == 8

