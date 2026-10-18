from dataclasses import replace
from typing import Any

import pytest

from agent_hub.models import gateway as gateway_module
from agent_hub.models.capacity import CapacityBackendError
from agent_hub.models.failure_receipt import get_gateway_failure_receipt
from agent_hub.models.gateway import GatewayCompletion, get_gateway_scope_diagnostic
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelResponse
from tests.unit.models.test_capacity import fingerprint
from tests.unit.models.test_capacity_diagnostic import FailingRedis
from tests.unit.models.test_capacity_diagnostic import diagnostic as capacity_diagnostic
from tests.unit.models.test_failure_receipt import OutcomesTransport, make_gateway
from tests.unit.models.test_gateway import CapacityStub, lease, request


def preserve(target: BaseException, source: object) -> None:
    helper = getattr(gateway_module, "preserve_gateway_scope_diagnostic", None)
    assert callable(helper), "scope diagnostic preservation API is missing"
    helper(target, source)


async def issued_failure() -> ModelTransportError:
    with pytest.raises(ModelTransportError) as caught:
        await make_gateway(
            OutcomesTransport([ModelTransportError("private provider body")]),
            CapacityStub([lease("primary")]),
        ).complete_with_context(request())
    assert get_gateway_scope_diagnostic(caught.value) is not None
    assert get_gateway_failure_receipt(caught.value) is not None
    return caught.value


async def test_preserve_rebinds_only_issued_diagnostic_without_other_authority() -> None:
    source = await issued_failure()
    source.__cause__ = RuntimeError("private cause")
    source.__context__ = RuntimeError("private context")
    source.__dict__.update(usage={"private": 123}, permission="allow", retryable=True)
    target = RuntimeError("existing runtime wrapper")
    target.__dict__["existing"] = "keep"
    args = target.args
    expected = get_gateway_scope_diagnostic(source)

    preserve(target, source)

    assert get_gateway_scope_diagnostic(target) is expected
    assert get_gateway_failure_receipt(target) is None
    assert target.args == args
    assert target.__cause__ is target.__context__ is None
    assert set(target.__dict__) == {"existing", gateway_module._SCOPE_DIAGNOSTIC_ATTRIBUTE}
    binding = target.__dict__[gateway_module._SCOPE_DIAGNOSTIC_ATTRIBUTE]
    assert binding[1] is target and binding[2] is expected
    assert all(value is not source for value in binding)
    source.__dict__.clear()
    assert get_gateway_scope_diagnostic(source) is None
    assert get_gateway_scope_diagnostic(target) is expected
    replay = RuntimeError("different wrapper")
    replay.__dict__.update(target.__dict__)
    assert get_gateway_scope_diagnostic(replay) is None


async def test_preserve_survives_multiple_runtime_wrappers() -> None:
    source = await issued_failure()
    first, second = RuntimeError("first"), RuntimeError("second")
    preserve(first, source)
    preserve(second, first)
    assert get_gateway_scope_diagnostic(second) is get_gateway_scope_diagnostic(source)
    assert get_gateway_failure_receipt(second) is None


@pytest.mark.parametrize("source", [None, object(), RuntimeError("private"), {"phase": "transport"}])
async def test_invalid_source_leaves_existing_target_unchanged(source: object) -> None:
    issued = await issued_failure()
    target = RuntimeError("wrapper")
    preserve(target, issued)
    before = dict(target.__dict__)
    preserve(target, source)
    assert target.__dict__ == before


async def test_preserve_rejects_replayed_binding_and_reconstructed_diagnostic() -> None:
    issued = await issued_failure()
    replay = RuntimeError("replayed source")
    replay.__dict__.update(issued.__dict__)
    original = get_gateway_scope_diagnostic(issued)
    assert original is not None
    forged = GatewayCompletion(
        response=ModelResponse(text="ok"), deployment_id="primary", logical_model="primary",
        provider_id="provider", provider_model="provider/primary",
        scope_diagnostic=replace(original),
    )
    for source in (replay, forged, original):
        target = RuntimeError("wrapper")
        preserve(target, source)
        assert target.__dict__ == {}
        assert get_gateway_scope_diagnostic(target) is None


async def test_preserve_accepts_issued_completion_diagnostic() -> None:
    issued = await issued_failure()
    completion = GatewayCompletion(
        response=ModelResponse(text="ok"), deployment_id="primary", logical_model="primary",
        provider_id="provider", provider_model="provider/primary",
        scope_diagnostic=get_gateway_scope_diagnostic(issued),
    )
    target = RuntimeError("wrapper")
    preserve(target, completion)
    assert get_gateway_scope_diagnostic(target) is get_gateway_scope_diagnostic(completion)
    assert get_gateway_failure_receipt(target) is None


async def test_preserve_keeps_optional_native_capacity_metadata() -> None:
    from agent_hub.models.capacity import CapacityPool
    from agent_hub.models.gateway import ModelGateway
    from agent_hub.models.registry import ModelRegistry
    from tests.unit.models.test_gateway import SecretStub, TransportStub, deployment

    selected = deployment("primary")
    root = CapacityPool(FailingRedis(2, TimeoutError("private")), deployments=[selected],
                        fingerprint_resolver=fingerprint)
    events: list[object] = []
    gateway = ModelGateway(ModelRegistry([selected]), root, SecretStub(events), TransportStub(events))
    with pytest.raises(CapacityBackendError) as caught:
        await gateway.complete(request())
    expected = capacity_diagnostic(caught.value)
    target = RuntimeError("wrapper")
    preserve(target, caught.value)
    scope = get_gateway_scope_diagnostic(target)
    assert scope is not None and scope.capacity_backend is expected
    assert capacity_diagnostic(target) is None
    assert get_gateway_failure_receipt(target) is None
    assert not events


async def test_preserve_ignores_hostile_source_and_target_properties() -> None:
    class Hostile(RuntimeError):
        def __getattribute__(self, name: str) -> Any:
            raise AssertionError("adapter attributes must not be consulted")

    source = await issued_failure()
    target = Hostile()
    preserve(target, source)
    assert get_gateway_scope_diagnostic(target) is get_gateway_scope_diagnostic(source)
    clean = RuntimeError("wrapper")
    preserve(clean, Hostile())
    assert clean.__dict__ == {}


async def test_preserve_failure_cannot_replace_runtime_outcome() -> None:
    class Unwritable(RuntimeError):
        @property
        def __dict__(self) -> dict[str, object]:  # type: ignore[override]
            raise RuntimeError("private adapter storage")

    source = await issued_failure()
    preserve(Unwritable("wrapper"), source)
