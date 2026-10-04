from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_hub.harness import project_validation_result as protocol

PROFILE = "ultra-load-storage-v1"


def result() -> dict[str, Any]:
    source = Path(__file__).resolve().parents[2] / "fixtures/project_business/ultra_storage_result.json"
    return dict(json.loads(source.read_text(encoding="utf-8")))


def test_composite_requires_complete_load_isolation_and_relocation() -> None:
    payload = result()
    validated = protocol.validate_scale_validation_result(payload)
    assert validated == payload and validated is not payload
    assert protocol.scale_validation_passed(payload)


def test_load_only_is_readable_but_cannot_satisfy_storage_profile() -> None:
    payload = result()["checks"]["load"]
    assert protocol.scale_validation_passed(payload)
    assert not protocol.scale_validation_passed(payload, expected_profile=PROFILE)


@pytest.mark.parametrize("name", ["load", "data_dir_isolation", "same_version_relocation"])
@pytest.mark.parametrize("mutation", ["missing", "extra", "failed", "unknown", "cleanup"])
def test_composite_rejects_omitted_or_failed_subchecks(name: str, mutation: str) -> None:
    payload = result()
    check = payload["checks"][name]
    if mutation == "missing":
        del payload["checks"][name]
    elif mutation == "extra":
        check["fake_pass"] = True
    elif mutation == "cleanup":
        check["cleanup_ok"] = False
    else:
        check.update(status=mutation, reasons=["observed failure"])
    assert not protocol.scale_validation_passed(payload)


@pytest.mark.parametrize("name", ["data_dir_isolation", "same_version_relocation"])
def test_each_success_measurement_is_required_and_typed(name: str) -> None:
    original = result()["checks"][name]["measurements"]
    invalid_values: tuple[object, ...] = (None, True, -1, "", [])
    for key in original:
        for value in invalid_values:
            if original[key] is value:
                continue
            payload = result()
            payload["checks"][name]["measurements"][key] = value
            assert not protocol.scale_validation_passed(payload), (name, key, value)
        payload = result()
        del payload["checks"][name]["measurements"][key]
        assert not protocol.scale_validation_passed(payload), (name, key)


@pytest.mark.parametrize("key", ["copied_data_sha256", "relocated_code_sha256"])
def test_storage_rejects_unequal_tree_digests(key: str) -> None:
    payload = result()
    payload["checks"]["same_version_relocation"]["measurements"][key] = "c" * 64
    assert not protocol.scale_validation_passed(payload)


def test_nested_failure_cannot_be_downgraded_to_unknown() -> None:
    payload = result()
    payload.update(status="unknown", reasons=["unavailable"])
    payload["checks"]["load"].update(status="failed", reasons=["wrong row"])
    assert not protocol.scale_validation_passed(payload)
    with pytest.raises(ValueError):
        protocol.validate_scale_validation_result(payload)


def test_storage_unknown_contains_no_invented_observations() -> None:
    payload = protocol.scale_validation_unknown("sandbox unavailable", profile=PROFILE)
    assert protocol.validate_scale_validation_result(payload, expected_profile=PROFILE) == payload
    assert not protocol.scale_validation_passed(payload, expected_profile=PROFILE)
    assert payload["profile"] == PROFILE
    checks: Any = payload["checks"]
    assert checks["same_version_relocation"]["measurements"]["old_paths_unavailable"] is False
    assert checks["same_version_relocation"]["measurements"]["copied_data_sha256"] is None
