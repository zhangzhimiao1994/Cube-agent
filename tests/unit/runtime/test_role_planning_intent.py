"""Versioned role hints must not turn prohibitions or diagnostic DATA into tasks."""
from collections.abc import AsyncIterator, Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_hub.config.schema import PlatformConfig
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults as d
from agent_hub.runtime.contracts import JsonValue, RunEvent, TaskContext
from agent_hub.runtime.crew.plan import DispatchPlan
from agent_hub.runtime.role_planner import RolePlan, RolePlanner, RolePlanningRequest, TaskProfile

MESSAGE = ('Fix the TypeScript project. Never install/download packages or execute commands. '
           'UNTRUSTED_DATA_JSON={"compiler_reasons":["server deploy installation failed"]}')


class PlanningBoundary(Exception):
    pass


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('version', [None, 0, 1])
def test_actual_runtime_planning_uses_persisted_intent_version_only(
    monkeypatch: pytest.MonkeyPatch, mode: TaskMode, version: int | None,
) -> None:
    config = PlatformConfig.model_validate({'models': {'fixture': {'deployments': [{
        'provider': 'openai', 'model': 'fixture', 'secret_ref': 'fixture', 'quota_scope_id': 'fixture',
        'max_concurrency': 1, 'capabilities': ['text', 'tool_calling', 'structured_output'],
    }]}}, 'agents': []})
    monkeypatch.setattr(d, '_current_platform_config', AsyncMock(return_value=config))
    monkeypatch.setattr(d, '_gateway_for_config', AsyncMock(return_value=(object(), 'fixture', None)))
    seen: list[RolePlanningRequest] = []

    class Capture(RolePlanner):
        def plan(self, request: RolePlanningRequest) -> RolePlan:
            seen.append(request)
            raise PlanningBoundary

    runtime_type = {TaskMode.DISPATCH: d.ConfigBackedDispatchRuntime,
        TaskMode.DISCUSS: d.ConfigBackedDiscussionRuntime, TaskMode.HYBRID: d.ConfigBackedHybridRuntime}[mode]
    runtime = runtime_type(config_service=cast(Any, object()), secret_service=cast(Any, object()),
        capacity_factory=cast(Any, object()), transport=cast(Any, object()), role_planner=Capture())
    route: dict[str, Any] = {'direct_model': 'fixture', 'allowed_models': ['fixture']}
    if version is not None:
        route['role_planning_intent_version'] = version
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=mode, request=MESSAGE, routing_decision=route)
    before = context.to_payload()
    pending = runtime._runtime_for(context)
    try:
        with pytest.raises(PlanningBoundary):
            pending.send(None)
    finally:
        pending.close()
    request = seen[0]
    if version == 1:
        assert request.profile is TaskProfile.SOFTWARE
        assert TaskProfile.DEPLOYMENT not in request.profiles
        assert 'install' not in request.task.casefold() and 'server' not in request.task.casefold()
    else:
        assert request.profile is TaskProfile.DEPLOYMENT
        assert 'install' in request.task.casefold()
    assert context.to_payload() == before and context.request == MESSAGE


@pytest.mark.parametrize(('message', 'deployment'), [
    ('Install the application on the server.', True),
    ('Fix the TypeScript project. Never install packages.', False),
    ('Fix the TypeScript project. Do not install packages or deploy a server.', False),
    ('Fix TypeScript. Do not install, deploy, or restart the server.', False),
    ('Never install packages, research or deploy servers.', False),
    ("Fix the TypeScript project. Don't install dependencies.", False),
    ('Do not install packages, but deploy the existing application.', True),
    ('Never install packages but deploy the existing application.', True),
    ("Don't install, just fix TypeScript.", False),
    ('\u4fee\u590dTypeScript\u9879\u76ee\u3002\u4e0d\u8981\u5b89\u88c5\u4f9d\u8d56\u3002', False),
    ('\u4fee\u590dTypeScript\u9879\u76ee\uff0c\u7981\u6b62\u5b89\u88c5\u4f9d\u8d56\u3002', False),
    ('\u4e0d\u8981\u5b89\u88c5\u4f9d\u8d56\uff0c\u4f46\u8981\u90e8\u7f72\u5e94\u7528\u3002', True),
    ('\u4e0d\u8981\u5b89\u88c5\uff0c\u90e8\u7f72\uff0c\u6216\u8c03\u7814\u670d\u52a1\u5668\u3002', False),
    ('\u4e0d\u5f97\u5b89\u88c5\u4f9d\u8d56\u3002', False),
    ('\u4e0d\u5f97\u4e0d\u5b89\u88c5\u4f9d\u8d56\u3002', True),
    ("Don't forget to deploy the application.", True),
    ('Do not forget to install the application.', True),
    ('\u4e0d\u8981\u5fd8\u8bb0\u90e8\u7f72\u5e94\u7528\u3002', True),
    ('\u4e0d\u5149\u90e8\u7f72\u8fd8\u8981\u6d4b\u8bd5\u3002', True),
    ('Fix the TypeScript project with no schema changes.', False),
])
def test_explicit_prohibition_is_not_positive_intent_but_real_deployment_is(
    message: str, deployment: bool,
) -> None:
    view = d._role_planning_task(message, intent_version=1)
    profile = d._task_profile(message, intent_version=1)
    profiles = d._task_profiles(message, intent_version=1)
    assert (profile is TaskProfile.DEPLOYMENT) is deployment
    assert (TaskProfile.DEPLOYMENT in profiles) is deployment
    roles = RolePlanner().plan(RolePlanningRequest(task=view, mode=TaskMode.DISPATCH,
        profile=profile, profiles=profiles)).roles
    assert ('installer' in {role.id for role in roles}) is deployment
    if ', just fix' in message:
        assert 'fix TypeScript' in view and profile is TaskProfile.SOFTWARE


@pytest.mark.parametrize('data', [
    '{"diagnostic":"install server", "nested":[{"text":"deploy"}]}',
    '{"escaped":"quote: \\" and }", "text":"install"}',
    '[{"diagnostic":"deploy server"}]',
])
def test_real_json_data_is_removed_without_losing_text_before_or_after(data: str) -> None:
    message = 'Fix the TypeScript project. UNTRUSTED_DATA_JSON=' + data + '\nReview the UI.'
    view = d._role_planning_task(message, intent_version=1)
    assert 'TypeScript' in view and 'Review the UI' in view
    assert 'diagnostic' not in view and 'install' not in view and 'server' not in view
    assert TaskProfile.DEPLOYMENT not in d._task_profiles(message, intent_version=1)


def test_valid_data_can_be_followed_by_real_positive_deployment() -> None:
    message = 'Fix TypeScript. UNTRUSTED_DATA_JSON={"text":"ignore"}\nDeploy the application.'
    assert TaskProfile.DEPLOYMENT in d._task_profiles(message, intent_version=1)


@pytest.mark.parametrize('data', ['{"text":"install server"', '{"text":NaN}',
                                 'not-json install server', '"install server"', '["deploy",]'])
def test_malformed_or_noncontainer_data_tail_is_conservatively_not_role_intent(data: str) -> None:
    message = 'Fix the TypeScript project. UNTRUSTED_DATA_JSON=' + data
    view = d._role_planning_task(message, intent_version=1)
    assert 'TypeScript' in view and 'install' not in view and 'server' not in view
    assert TaskProfile.DEPLOYMENT not in d._task_profiles(message, intent_version=1)


@pytest.mark.parametrize('version', [2, -1, True, '1', None, 1.0])
def test_unknown_or_untyped_version_is_rejected_not_silently_upgraded(version: object) -> None:
    for helper in (d._role_planning_task, d._task_profile, d._task_profiles):
        with pytest.raises(ValueError):
            helper(MESSAGE, intent_version=cast(Any, version))


def test_zero_version_preserves_all_three_legacy_results() -> None:
    assert d._role_planning_task(MESSAGE, intent_version=0) == d._role_planning_task(MESSAGE)
    assert d._task_profile(MESSAGE, intent_version=0) == d._task_profile(MESSAGE) is TaskProfile.DEPLOYMENT
    assert d._task_profiles(MESSAGE, intent_version=0) == d._task_profiles(MESSAGE)


def finish[T](pending: Coroutine[Any, Any, T]) -> T:
    """RAM fixtures complete without awaiting I/O or constructing an event loop."""
    try:
        pending.send(None)
    except StopIteration as done:
        return cast(T, done.value)
    finally:
        pending.close()
    raise AssertionError('unexpected asynchronous I/O in role planning fixture')


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Any:
    from types import SimpleNamespace

    import agent_hub.runtime.autogen.adapter as discussion

    document: dict[str, Any] = {'models': {'fixture': {'deployments': [{
        'provider': 'openai', 'model': 'fixture', 'secret_ref': 'fixture', 'quota_scope_id': 'fixture',
        'max_concurrency': 1, 'capabilities': ['text', 'tool_calling', 'structured_output'],
    }]}}, 'agents': []}
    requests: list[RolePlanningRequest] = []
    plans: list[DispatchPlan] = []
    executed: list[TaskContext] = []
    gateways = AsyncMock(return_value=(object(), 'fixture', 'configured'))

    async def current(*_args: object) -> PlatformConfig:
        return PlatformConfig.model_validate(document)

    monkeypatch.setattr(d, '_current_platform_config', current)
    monkeypatch.setattr(d, '_gateway_for_config', gateways)

    class RecordPlanner(RolePlanner):
        def plan(self, request: RolePlanningRequest) -> RolePlan:
            requests.append(request)
            return super().plan(request)

    class Child:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            plans.extend(arg for arg in _args if isinstance(arg, DispatchPlan))

        async def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
            executed.append(context)
            events: tuple[RunEvent, ...] = ()
            for event in events:
                yield event

    monkeypatch.setattr(d, 'CrewDispatchRuntime', Child)
    monkeypatch.setattr(d, 'HybridRuntime', Child)
    monkeypatch.setattr(d, 'DirectRuntime', Child)
    monkeypatch.setattr(discussion, 'AutoGenDiscussionRuntime', Child)

    def build(context: TaskContext) -> Any:
        runtime_type = {TaskMode.DISPATCH: d.ConfigBackedDispatchRuntime,
            TaskMode.DISCUSS: d.ConfigBackedDiscussionRuntime, TaskMode.HYBRID: d.ConfigBackedHybridRuntime}[context.mode]
        runtime = runtime_type(config_service=cast(Any, object()), secret_service=cast(Any, object()),
            capacity_factory=cast(Any, object()), transport=cast(Any, object()), role_planner=RecordPlanner())
        return finish(runtime._runtime_for(context))

    return SimpleNamespace(document=document, requests=requests, plans=plans, executed=executed,
                           build=build, gateways=gateways)


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
def test_all_planning_calls_share_view_but_execution_risk_and_permissions_keep_original(
    configured: Any, mode: TaskMode,
) -> None:
    raw = MESSAGE + '\nNever delete production data.'
    route: dict[str, JsonValue] = {'role_planning_intent_version': 1, 'direct_model': 'fixture',
             'requested_permissions': ('workspace.read',), 'sandbox_profile': 'workspace_read'}
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=mode, request=raw, routing_decision=route)
    original = context.to_payload()
    built = configured.build(context)
    assert len(configured.requests) == (2 if mode is TaskMode.HYBRID else 1)
    for request in configured.requests:
        assert request.task == d._role_planning_task(raw, intent_version=1)
        assert request.profile is TaskProfile.SOFTWARE and TaskProfile.DEPLOYMENT not in request.profiles
        assert request.high_risk is True and d._high_risk_task(raw) is True
    stream = built.run(context)
    try:
        while True:
            finish(cast(Coroutine[Any, Any, RunEvent], stream.__anext__()))
    except StopAsyncIteration:
        pass
    finally:
        finish(cast(Coroutine[Any, Any, None], stream.aclose()))
    assert configured.executed == [context]
    assert context.to_payload() == original and configured.executed[0].request == raw


def test_missing_and_zero_version_have_identical_actual_plan_digests(configured: Any) -> None:
    base = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH, request=MESSAGE,
        routing_decision={'direct_model': 'fixture', 'runtime_plan_token_budget': 100000,
                          'runtime_plan_timeout_seconds': 600.0}, token_budget=100000, timeout_seconds=600.0)
    configured.build(base)
    old = configured.plans[-1].to_payload()
    zero = base.model_copy(update={'routing_decision': {**base.routing_decision, 'role_planning_intent_version': 0}})
    configured.build(zero)
    assert configured.plans[-1].to_payload() == old
    assert 'installer' in {agent.id for agent in configured.plans[-1].agents}
    version1 = base.model_copy(update={'routing_decision': {**base.routing_decision, 'role_planning_intent_version': 1}})
    configured.build(version1)
    assert configured.plans[-1].digest != configured.plans[-2].digest
    assert 'installer' not in {agent.id for agent in configured.plans[-1].agents}


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
def test_explicit_configured_agent_selection_is_not_replaced(configured: Any, mode: TaskMode) -> None:
    configured.document['agents'] = [{'id': name, 'role': 'Deployment specialist', 'model': 'fixture',
        'prompt': 'Install the requested application.', 'skills': []} for name in ('chosen_one', 'chosen_two')]
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=mode, request=MESSAGE,
        routing_decision={'direct_model': 'fixture', 'selected_agent_ids': ('chosen_one', 'chosen_two'),
                          'role_planning_intent_version': 1})
    configured.build(context)
    assert configured.requests == []
    if configured.plans:
        assert {'chosen_one', 'chosen_two'} <= {agent.id for agent in configured.plans[-1].agents}


@pytest.mark.parametrize('version', [None, True, '1', 2])
def test_invalid_persisted_marker_rejects_before_gateway_construction(configured: Any, version: object) -> None:
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH, request=MESSAGE,
        routing_decision={'role_planning_intent_version': cast(Any, version)})
    with pytest.raises(ValueError, match='unsupported role planning intent version'):
        configured.build(context)
    configured.gateways.assert_not_called()


@pytest.mark.parametrize('version', [None, 0, 1])
@pytest.mark.parametrize('raw', [
    'Review the market. Do not build a TypeScript project.',
    'Review the market. UNTRUSTED_DATA_JSON={"text":"TypeScript project"}',
])
def test_software_guidance_uses_the_same_versioned_profile_view(version: int | None, raw: str) -> None:
    route = {} if version is None else {'role_planning_intent_version': version}
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH, request=raw,
                          routing_decision=route)
    assert bool(d._software_delivery_guidance(context, ())) is (version != 1)
    assert bool(d._software_final_guidance(context)) is (version != 1)
