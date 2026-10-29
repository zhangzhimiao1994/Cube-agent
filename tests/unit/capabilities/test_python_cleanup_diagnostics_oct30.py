"""Safe retry diagnostics only; no native execution, database or cache mutation."""

from __future__ import annotations

import copy
import json
import logging
from itertools import pairwise
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest

from agent_hub.capabilities import python_project_lifecycle as lifecycle
from agent_hub.capabilities.project_baselines import (
    ProjectBaselineError,
    ProjectBaselineReference,
    ProjectBaselineStore,
)
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation
from tests.unit.capabilities.test_python_project_lifecycle import Rows, Session
from tests.unit.skills.sandbox.test_python_project_client import invocation

SECRET = "private-token SQL SELECT model-body user-source /private/customer.py\nSECRET_LINE"


def hidden_baseline_error() -> ProjectVerificationError:
    """Real load -> _invocation ValueError, hidden twice with `from None`."""
    try:
        ProjectBaselineStore(Path("private-relative-root")).load(
            base_python_invocation(invocation()),
            ProjectBaselineReference("a" * 64, "b" * 64, 1),
        )
    except ProjectBaselineError:
        try:
            raise ProjectVerificationError(SECRET) from None
        except ProjectVerificationError as error:
            return error
    raise AssertionError("relative baseline root unexpectedly accepted")


def diagnosis(error: BaseException) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(lifecycle._retry_failure_diagnosis(error)))


def test_from_none_preserves_real_baseline_root_module_function_and_line() -> None:
    error = hidden_baseline_error()
    assert error.__suppress_context__
    assert error.__context__ is not None and error.__context__.__suppress_context__
    result = diagnosis(error)
    assert [item["type"] for item in result["exceptions"]] == [
        "ProjectVerificationError", "ProjectBaselineError", "ValueError",
    ]
    root = error.__context__.__context__
    assert root is not None and root.__traceback__ is not None
    tb = root.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    assert result["exceptions"][-1]["frames"][-1] == {
        "module": "capabilities/project_baselines.py",
        "function": "_invocation",
        "line": tb.tb_lineno,
    }
    assert not result["truncated"]
    raw = json.dumps(result)
    assert SECRET not in raw and "private-relative-root" not in raw
    assert "customer.py" not in raw and "SECRET_LINE" not in raw


def test_exception_message_notes_repr_and_arbitrary_class_name_are_never_logged() -> None:
    class Hostile(Exception):
        def __str__(self) -> str:
            raise AssertionError("must not stringify exceptions")

        def __repr__(self) -> str:
            raise AssertionError("must not repr exceptions")

    Hostile.__name__ = SECRET
    error = Hostile(SECRET)
    error.add_note(SECRET)
    assert diagnosis(error) == {
        "schema_version": 1,
        "exceptions": [{"type": "Exception", "frames": []}],
        "truncated": False,
    }


def test_explicit_cause_wins_over_context_and_exception_cycles_stop() -> None:
    first, second = RuntimeError(SECRET), ValueError(SECRET)
    first.__cause__ = second
    first.__context__ = TypeError(SECRET)
    second.__context__ = first
    result = diagnosis(first)
    assert [entry["type"] for entry in result["exceptions"]] == ["RuntimeError", "ValueError"]
    assert result["truncated"]


def test_exception_depth_is_capped_at_four() -> None:
    errors = [ValueError(SECRET) for _ in range(8)]
    for current, following in pairwise(errors):
        current.__context__ = following
    result = diagnosis(errors[0])
    assert len(result["exceptions"]) == 4
    assert result["truncated"]


def test_twelve_frame_budget_is_global_and_keeps_innermost_root_frames() -> None:
    original = hidden_baseline_error()
    baseline = original.__context__
    assert baseline is not None and baseline.__context__ is not None
    root = baseline.__context__
    assert root.__traceback__ is not None
    tb = root.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    chain = None
    for line in range(1000, 1030):
        chain = TracebackType(chain, tb.tb_frame, tb.tb_lasti, line)
    original.__traceback__ = chain
    baseline.__traceback__ = chain
    result = diagnosis(original)
    frames = [frame for error in result["exceptions"] for frame in error["frames"]]
    assert len(frames) == 12
    assert frames[-1]["function"] == "_invocation"
    assert frames[-1]["line"] == tb.tb_lineno
    assert result["truncated"]


@pytest.mark.parametrize("filename", [
    "/private/workspaces/agent_hub/stolen.py",
    "C:/private/workspaces/agent_hub/stolen.py",
    str(Path(lifecycle.__file__).parent / ".." / "private-source.py"),
])
def test_external_or_spoofed_agent_hub_user_source_frames_are_omitted(filename: str) -> None:
    namespace: dict[str, Any] = {
        "__name__": lifecycle.__name__, "__file__": filename, "SECRET": SECRET,
    }
    try:
        # Fixed test code, with a synthetic filename; no user file is read/executed.
        exec(compile("raise ValueError(SECRET)", filename, "exec"), namespace)  # noqa: S102
    except ValueError as error:
        result = diagnosis(error)
    assert result["exceptions"] == [{"type": "ValueError", "frames": []}]


class RetrySession(Session):
    async def scalars(self, query: Any) -> Any:
        if query.column_descriptions[0]["name"] == "RunRow":
            return Rows([self.row])
        return await super().scalars(query)


@pytest.mark.parametrize("hostile_name", [False, True])
async def test_retry_warning_adds_single_line_safe_json_without_changing_cleanup_state(
    caplog: pytest.LogCaptureFixture, hostile_name: bool,
) -> None:
    session = RetrySession()
    before = copy.deepcopy(vars(session.row))
    error: Exception = hidden_baseline_error()
    if hostile_name:
        error = type(SECRET, (Exception,), {})(SECRET)

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader:
        async def python_expired_environment_invocation(self, *args: Any) -> Any:
            raise AssertionError("diagnostics must not enter capacity maintenance")

        async def python_original_context(self, *args: Any) -> Any:
            assert session.open
            raise error

    class ForbiddenExecutor:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("diagnostics must not permit cleanup")

    hook = lifecycle.PythonProjectTerminalHook(Repository(), Reader(), ForbiddenExecutor())
    with caplog.at_level(logging.WARNING, logger=lifecycle.__name__):
        assert await hook.retry_completed() == 1
    assert vars(session.row) == before
    assert hook._retry_cursor == session.row.id
    assert len(caplog.records) == 1
    record = caplog.records[0]
    text = record.getMessage()
    expected = "Exception" if hostile_name else "ProjectVerificationError"
    assert text.startswith(
        f"python_project_cleanup_retry_failed run_id={session.row.id} error_type={expected} diagnosis="
    )
    payload = json.loads(text.split(" diagnosis=", 1)[1])
    assert payload["exceptions"][0]["type"] == expected
    assert record.exc_info is None and record.stack_info is None
    assert SECRET not in text and "SECRET_LINE" not in text and "\n" not in text
    assert str(Path(lifecycle.__file__).parent) not in text
