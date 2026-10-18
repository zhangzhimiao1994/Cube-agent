from dataclasses import replace

import pytest

from agent_hub.runtime.instruction_context import model_request_sha256
from tests.contracts.test_required_tool_transport import request


@pytest.mark.parametrize("native,legacy", [
    (False, "4a667c987250c77576376fea207648da87b10442dcc7745a7bc26fc8d88b8aa4"),
    (True, "1bab8ecc198635caf529f7f2fe791af1d479ffd097c99a8986d3f4efd0e6b0bd"),
])
def test_required_tool_none_preserves_captured_request_hash(native: bool, legacy: str) -> None:
    req = request("chat_completions", native_tool_continuation=native)
    assert model_request_sha256(req) == legacy
    assert model_request_sha256(replace(req, required_tool=None)) == legacy


@pytest.mark.parametrize("native", [False, True])
def test_required_tool_binds_submitted_and_native_continuation_requests(native: bool) -> None:
    req = request("chat_completions", native_tool_continuation=native)
    verify = replace(req, required_tool="project_verify")
    inspect = replace(req, required_tool="project_inspect")
    assert len({model_request_sha256(value) for value in (req, verify, inspect)}) == 3
