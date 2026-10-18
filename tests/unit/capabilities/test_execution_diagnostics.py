from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.skills.sandbox.base import SkillResult


def test_diagnostic_rejects_arbitrary_payload_instead_of_a_process_result() -> None:
    with pytest.raises(TypeError, match="requires a SkillResult"):
        ExecutionDiagnostic(backend="systemd", result=cast(SkillResult, {"stderr": "private"}))
    with pytest.raises(TypeError, match="must be ExecutionDiagnostic"):
        RuntimeCapabilityError(
            "skill execution failed",
            execution_diagnostic=cast(ExecutionDiagnostic, {"stderr": "private"}),
        )


def test_diagnostic_rejects_unconfirmed_process_result() -> None:
    with pytest.raises(ValueError, match="requires a completed result"):
        ExecutionDiagnostic(
            backend="systemd",
            result=SkillResult(exit_code=None, timed_out=False, stdout="", stderr=""),
        )


def test_diagnostic_cannot_be_mutated_to_bypass_sanitization() -> None:
    diagnostic = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(
            exit_code=1, timed_out=False, stdout="progress", stderr="API_KEY=private-value",
        ),
    )
    with pytest.raises(FrozenInstanceError):
        diagnostic.backend = "API_KEY=private-value"  # type: ignore[misc]
    payload = diagnostic.to_payload()
    assert isinstance(payload, Mapping)
    assert payload["stdout"] == "progress"
    assert payload["stderr"] == ""
    assert "private-value" not in repr(diagnostic)
    assert "private-value" not in repr(payload)


@pytest.mark.parametrize("backend", ["API_KEY=private-value", "api_key", "unsafe\nbackend"])
def test_diagnostic_does_not_copy_unsafe_backend_metadata(backend: str) -> None:
    diagnostic = ExecutionDiagnostic(
        backend=backend,
        result=SkillResult(exit_code=1, timed_out=False, stdout="", stderr=""),
    )
    assert diagnostic.to_payload()["backend"] == "unknown"
    assert "private-value" not in repr(diagnostic)


@pytest.mark.parametrize("size", [4095, 4096, 4097])
def test_output_limit_applies_to_utf8_bytes_including_visible_marker(size: int) -> None:
    output = "x" * size
    payload = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=1, timed_out=False, stdout=output, stderr=""),
    ).to_payload()
    text = payload["stdout"]
    metadata = payload["stdout_metadata"]
    assert isinstance(text, str)
    assert isinstance(metadata, Mapping)
    assert len(text.encode("utf-8")) <= 4096
    if size <= 4096:
        assert text == output
        assert metadata["truncated"] is False
    else:
        assert text.endswith("\n[truncated]")
        assert metadata["truncated"] is True


@pytest.mark.parametrize(
    "output",
    [
        pytest.param('{"password":\n"synthetic-private-value"}\n', id="multiline-json"),
        pytest.param(
            'before\n{"api_key": {\n"value": "synthetic-private-value"\n}}\nafter',
            id="nested-sensitive-block",
        ),
        pytest.param(
            "before\n-----BEGIN PRIVATE KEY-----\nsynthetic-private-value\n"
            "-----END PRIVATE KEY-----\nafter", id="pem-private-key",
        ),
        pytest.param(
            "before\n-----BEGIN RSA PRIVATE KEY-----\nsynthetic-private-value\n"
            "-----END RSA PRIVATE KEY-----\nafter", id="pem-rsa-private-key",
        ),
        pytest.param(
            "failed https://alice:synthetic-private-value@example.invalid/api\nafter",
            id="credentialed-url",
        ),
    ],
)
def test_complex_sensitive_output_hides_entire_stream(output: str) -> None:
    payload = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=1, timed_out=False, stdout=output, stderr=output),
    ).to_payload()
    assert "synthetic-private-value" not in repr(payload)
    for stream in ("stdout", "stderr"):
        assert payload[stream] == ""
        metadata = payload[f"{stream}_metadata"]
        assert isinstance(metadata, Mapping)
        assert metadata["redacted"] is True
        redacted_lines = metadata["redacted_lines"]
        assert isinstance(redacted_lines, int)
        assert redacted_lines > 0
        assert metadata["sanitized_bytes"] == 0


def test_plain_traceback_and_url_without_credentials_remain_available() -> None:
    stderr = (
        'Traceback (most recent call last):\n  File "main.py", line 1, in <module>\n'
        "ModuleNotFoundError: No module named 'PIL'\n"
        "source: https://example.invalid/path@label\n"
    )
    payload = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=1, timed_out=False, stdout="", stderr=stderr),
    ).to_payload()
    assert payload["stderr"] == stderr


@pytest.mark.parametrize(
    "sensitive_line",
    [
        pytest.param(
            "\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44=synthetic-private-value",
            id="nfkc-fullwidth-key",
        ),
        pytest.param("pass\x1b[0mword=synthetic-private-value", id="ansi-key"),
        pytest.param("pass\u200bword=synthetic-private-value", id="format-control-key"),
        pytest.param("pass\x00word=synthetic-private-value", id="nul-key"),
        pytest.param("pass\x0bword=synthetic-private-value", id="vertical-tab-key"),
        pytest.param("pass\x1b]0;title\x07word=synthetic-private-value", id="ansi-osc-key"),
    ],
)
def test_detection_view_redacts_original_obfuscated_sensitive_lines(sensitive_line: str) -> None:
    output = f"before\n{sensitive_line}\nafter"
    payload = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=1, timed_out=False, stdout=output, stderr=output),
    ).to_payload()
    assert payload["stdout"] == "before\nafter"
    assert payload["stderr"] == "before\nafter"
    assert "synthetic-private-value" not in repr(payload)


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(
            '{"pass\x1b[0mword":\x1b[0m\n"synthetic-private-value"}\n',
            id="ansi-multiline-field",
        ),
        pytest.param(
            '{"password":\u200b\n"synthetic-private-value"}\n',
            id="format-control-multiline-field",
        ),
        pytest.param(
            "-----BEGIN PRI\x1b[0mVATE KEY-----\nsynthetic-private-value\n"
            "-----END PRIVATE KEY-----", id="ansi-pem",
        ),
        pytest.param(
            "https://alice:synthetic-private-value\u200b@example.invalid", id="format-control-url",
        ),
        pytest.param(
            "https\uff1a\uff0f\uff0falice\uff1asynthetic-private-value\uff20example.invalid",
            id="nfkc-credentialed-url",
        ),
    ],
)
def test_complex_sensitive_blocks_use_normalized_detection_view(output: str) -> None:
    payload = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=1, timed_out=False, stdout=output, stderr=output),
    ).to_payload()
    assert payload["stdout"] == ""
    assert payload["stderr"] == ""
    assert "synthetic-private-value" not in str(payload["stdout"])
    assert "synthetic-private-value" not in str(payload["stderr"])
    assert "synthetic-private-value" not in repr(payload)


def test_normalization_is_for_detection_only_and_preserves_safe_original_text() -> None:
    output = "\uff21\uff22\uff23\x1b[0m\u200b \u4e2d\u6587\n"
    payload = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=1, timed_out=False, stdout=output, stderr=output),
    ).to_payload()
    assert payload["stdout"] == output
    assert payload["stderr"] == output


@pytest.mark.parametrize(
    "content",
    [
        "password=synthetic-private-value",
        "https://alice:synthetic-private-value@example.invalid",
        "-----BEGIN PRIVATE KEY-----\nsynthetic-private-value\n-----END PRIVATE KEY-----",
        "pass\u200bword=synthetic-private-value",
        "pass\x00word=synthetic-private-value",
    ],
    ids=["sensitive-field", "credentialed-url", "private-key", "zero-width-key", "nul-key"],
)
def test_ansi_sequence_contents_are_also_checked_before_keeping_original_text(content: str) -> None:
    output = f"before\n\x1b]0;{content}\x07\nafter"
    payload = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=1, timed_out=False, stdout=output, stderr=output),
    ).to_payload()
    assert payload["stdout"] == ""
    assert payload["stderr"] == ""
    assert "synthetic-private-value" not in repr(payload)
