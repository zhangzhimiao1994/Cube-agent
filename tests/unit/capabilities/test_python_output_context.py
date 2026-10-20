"""Context loss must never turn a sensitive stream into a safe tail preview."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json

import pytest

from agent_hub.skills.sandbox.python_project_process import (
    _Capture,
    python_phase_capture_limit,
    python_root_response_limit,
)
from tests.unit.capabilities.test_python_project_reports import decode, fail, fixture

_WITHHELD = "[truncated output withheld: context lost]\n"


def secret_stream(kind: str) -> bytes:
    progress = "progress ok\n" * 4000
    if kind == "half_line":
        value = "API_KEY=" + "q" * 60000 + "secret-suffix-sentinel\n"
    elif kind == "multiline":
        value = "API_KEY=|\n" + "  secret-suffix-sentinel\n" * 6000
    else:
        value = 'API_KEY={\n' + '  "value": "secret-suffix-sentinel",\n' * 6000 + "}\n"
    return (progress + value + "ActualFailure: mismatch\n").encode()


@pytest.mark.parametrize("kind", ["half_line", "multiline", "structured"])
@pytest.mark.parametrize("chunk_size", [1, 7, 16384, 1000000])
def test_capture_hides_all_context_lost_output_across_chunks(kind: str, chunk_size: int) -> None:
    raw = secret_stream(kind)
    capture = _Capture(32768)
    for start in range(0, len(raw), chunk_size):
        capture.feed(raw[start : start + chunk_size])
    assert capture.truncated and capture.observed == len(raw)
    assert capture.text() == _WITHHELD
    assert not capture.head and not capture.tail


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_decoder_distrusts_legacy_truncated_preview_and_hashes_only_disclosure(stream: str) -> None:
    api = importlib.import_module("agent_hub.capabilities.python_project_reports")
    context = fixture()
    invocation, _, _, raw = context
    limit = python_phase_capture_limit(invocation.output_limit_bytes)
    # The key and the beginning of a multiline value were discarded by an old reader.
    preview = "progress ok\n" * 1000 + "\n[truncated]\n" + "  secret-suffix-sentinel\n" * 200
    fail(raw, 2, **{stream: preview, stream + "_truncated": True,
                   stream + "_observed_bytes": limit + 100000})
    failure = decode(api, context)
    payload = failure.diagnostic.to_payload()
    assert payload[stream] == _WITHHELD
    metadata = payload[stream + "_metadata"]
    assert metadata["sha256"] == hashlib.sha256(_WITHHELD.encode()).hexdigest()
    assert metadata["sanitized_bytes"] == metadata["returned_bytes"] == len(_WITHHELD.encode())
    assert metadata["truncated"] is False  # Describes the disclosed replacement, not the raw stream.
    assert "secret-suffix-sentinel" not in json.dumps(payload)
    assert "secret-suffix-sentinel" not in json.dumps(failure.to_payload(), default=dict)


@pytest.mark.parametrize("limit", [1, 2, 8, 32, 4096, 32768])
def test_withheld_capture_still_obeys_shared_root_frame_budget(limit: int) -> None:
    capture = _Capture(limit)
    capture.feed(b"x" * (limit + 1))
    assert capture.text() == _WITHHELD[:limit]
    body = json.dumps({"checks": [{"stdout": capture.text(), "stderr": capture.text()}] * 3})
    envelope = json.dumps({"result": {"stdout": body}})
    assert len(envelope.encode()) < python_root_response_limit(1)


def test_complete_stream_keeps_actual_error_and_full_context_redaction() -> None:
    api = importlib.import_module("agent_hub.capabilities.python_project_reports")
    context = fixture()
    capture = _Capture(32768)
    raw = ("progress ok\n" * 500 + "API_KEY=small-secret-sentinel\nActualFailure: mismatch\n").encode()
    for start in range(0, len(raw), 7):
        capture.feed(raw[start : start + 7])
    assert not capture.truncated and capture.text() == raw.decode()
    fail(context[-1], 2, stderr=capture.text(), stderr_observed_bytes=capture.observed)
    payload = decode(api, context).diagnostic.to_payload()
    assert payload["stderr"].endswith("ActualFailure: mismatch\n")
    assert "small-secret-sentinel" not in payload["stderr"]
    assert payload["stderr_metadata"]["redacted_lines"] == 1


def test_native_capture_regression_api_checks_inner_and_outer_protocols() -> None:
    name = "tests.unit.capabilities.python_capture_native_regression"
    assert importlib.util.find_spec(name) is not None, "native capture regression API missing"
    probe = importlib.import_module(name)
    result = probe.run_native_capture_regression(inputs=fixture()[1])
    assert result["fixture_only"] is True
    assert result["project_code_executed"] is False
    assert [case["actual_error_visible"] for case in result["cases"]] == [False, False, True]
    for case in result["cases"]:
        assert case["phase_stdout_json_intact"] is True
        assert case["raw_report_complete"] is True
        assert case["root_envelope_roundtrip"] is True
        assert case["secret_visible"] is False
