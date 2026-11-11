"""Keep role tools, authorized catalog and real gateway admission in agreement."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

import agent_hub.runtime.review_assembly as review
from agent_hub.config.schema import PlatformConfig
from agent_hub.domain.runs import TaskMode
from agent_hub.models.routing_policy import DeploymentRoutingConstraint
from agent_hub.models.types import ModelCapability, ModelMessage, ModelRequest, ToolDefinition
from agent_hub.runtime import defaults
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    AvailableMcpManifestCapabilityGateway,
    FakeConfigService,
    FakeSecretService,
    FakeTransport,
    _immediate_capacity,
)


def config(*, qwen_tools=True):
    def deployment(provider, capabilities):
        return {'provider': provider, 'model': provider+'-chat', 'api_base': 'https://example.test/v1',
                    'credential_ref': 'secret://'+provider, 'quota_scope_id': provider,
                    'max_concurrency': 2, 'context_window_tokens': 100_000, 'capabilities': capabilities}
    return PlatformConfig.model_validate({'models': {
        'deepseek': {'deployments': [deployment('deepseek', ['text', 'tool_calling', 'structured_output'])]},
        'qwen': {'deployments': [deployment('qwen', ['text', 'tool_calling', 'structured_output']
                                          if qwen_tools else ['text'])]},
    }, 'agents': []})


def role(name='reader', *, tools=()):
    return RoleAssignment(id=name, role='Analyst', purpose=RolePurpose.EXPERTISE,
        mission='Compare the available evidence.', must_answer=('What is supported?',),
        allowed_tools=tools, forbidden_actions=('Do not access outside the authorized workspace.',),
        skills=(), output_schema={}, model='qwen')


@contextmanager
def review_scope(platform, ids=('deepseek_1',)):
    """Exercise the real review ContextVar/filter, with a local no-I/O budget."""
    token = review._execution.set(SimpleNamespace(
        budget=SimpleNamespace(tenant_id=TENANT_ID, check=lambda: None),
        snapshot=SimpleNamespace(platform=platform,
            contracts=tuple(SimpleNamespace(deployment_id=id_) for id_ in ids)),
    ))
    try:
        yield
    finally:
        review._execution.reset(token)


@pytest.mark.asyncio
async def test_review_filtered_role_matches_context_fit_and_actual_gateway(monkeypatch):
    """Raw qwen CAN call tools, but is NOT in this execution's authorized catalog."""
    monkeypatch.setattr(review, 'review_deployments',
                        lambda rows: tuple(row for row in rows if row.id == 'deepseek_1'))
    platform = config()
    source_roles = (role(tools=('workspace.read',)),)
    assigned = defaults._assign_models_to_roles(source_roles, platform, default_model='deepseek',
        task='Read evidence', role_tools_by_id={'reader': ('workspace.read',)})
    assert assigned[0].model == 'deepseek'
    matrix, _ = defaults._role_model_routing_matrix_payload(source_roles, assigned, platform,
        default_model='deepseek', task='Read evidence', role_tools_by_id={'reader': ('workspace.read',)})
    assert matrix[0]['selected_logical_model'] == 'deepseek'
    fallbacks = defaults._role_model_fallbacks_by_id(source_roles, assigned, platform,
        default_model='deepseek', task='Read evidence', role_tools_by_id={'reader': ('workspace.read',)})
    assert 'qwen' not in fallbacks.get('reader', ())
    transport = FakeTransport()
    gateway, _, _ = await defaults._gateway_for_config(platform, tenant_id=TENANT_ID,
        secret_service=FakeSecretService(), capacity_factory=_immediate_capacity, transport=transport)
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISCUSS,
                          request='Read evidence')
    from agent_hub.runtime.autogen.adapter import AutoGenDiscussionRuntime
    plan = defaults._discussion_plan((assigned[0], replace(role('critic'), model='deepseek')), 'deepseek')
    runtime = AutoGenDiscussionRuntime(gateway, plan)
    request = ModelRequest(logical_model=assigned[0].model,
        messages=(ModelMessage(role='user', content=runtime._task_text(context)),),
        required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}),
        tools=(ToolDefinition(name='read', description='Read permitted evidence',
                              parameters={'type': 'object', 'properties': {}}),))
    fitted = runtime._fit_handoff_request(request, context, actor='reader', owned_task_positions=(0,))
    assert gateway.context_window_tokens(fitted) == 100_000
    await gateway.complete_with_context(fitted)
    assert len(transport.calls) == 1 and transport.calls[0][0].id == 'deepseek_1'


@pytest.mark.asyncio
async def test_final_discussion_owner_tools_are_bound_before_model_selection():
    """Truncating nine roles must not give the new request owner stale qwen routing."""
    platform = config(qwen_tools=False)
    class Planner:
        def plan(self, request):
            return SimpleNamespace(roles=tuple(role('analyst'+str(i)) for i in range(9)))
    factory = defaults.ConfigBackedDiscussionRuntime(config_service=FakeConfigService(platform.model_dump()),
        secret_service=FakeSecretService(), capacity_factory=_immediate_capacity,
        transport=FakeTransport(), role_planner=Planner(),
        capability_gateway=AvailableMcpManifestCapabilityGateway())
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=TaskMode.DISCUSS,
                          request='Use search.web_search to compare the evidence.')
    planned = await factory._runtime_for(context)
    participants = planned._child._plan.participants
    owner = next(p for p in participants if 'search.web_search' in p.allowed_tools)
    assert owner.id == 'analyst7' and owner.logical_model == 'deepseek'
    assert any(p.logical_model == 'qwen' and not p.allowed_tools for p in participants)


def test_review_filter_never_reintroduces_disallowed_explicit_deployment(monkeypatch):
    platform = config()
    monkeypatch.setattr(review, 'review_deployments',
                        lambda rows: tuple(row for row in rows if row.id == 'deepseek_1'))
    constraint = DeploymentRoutingConstraint(logical_model='qwen', provider='qwen', model='qwen-chat')
    with pytest.raises(defaults.HarnessModelSelectionError):
        defaults._assign_models_to_roles((role(tools=('workspace.read',)),), platform,
            default_model='qwen', task='Read evidence', deployment_constraint=constraint)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,factory_type', [
    (TaskMode.DIRECT, defaults.ConfigBackedDirectRuntime),
    (TaskMode.DISPATCH, defaults.ConfigBackedDispatchRuntime),
    (TaskMode.DISCUSS, defaults.ConfigBackedDiscussionRuntime),
    (TaskMode.HYBRID, defaults.ConfigBackedHybridRuntime),
])
async def test_all_modes_share_review_catalog_for_real_roles_fallbacks_and_capacity(mode, factory_type):
    platform = config()
    # This configured edge must be removed, never bridged to some new alias.
    platform.models['deepseek'].fallback_model = 'qwen'
    seen = []
    async def capacity(tenant_id, deployments):
        seen.append(deployments)
        return await _immediate_capacity(tenant_id, deployments)
    class Planner:
        def plan(self, request):
            roles = (role('analyst'), role('critic'))
            if request.mode is not TaskMode.DISCUSS:
                roles = tuple(replace(item, output_schema={'summary': 'string'}) for item in roles)
            return SimpleNamespace(roles=roles)
    kwargs = {'config_service': FakeConfigService(platform.model_dump()),
        'secret_service': FakeSecretService(), 'capacity_factory': capacity, 'transport': FakeTransport(),
        'capability_gateway': AvailableMcpManifestCapabilityGateway()}
    if mode is not TaskMode.DIRECT:
        kwargs['role_planner'] = Planner()
    context = TaskContext(run_id=uuid4(), tenant_id=TENANT_ID, mode=mode,
        request='Use search.web_search to compare the evidence.')
    with review_scope(platform):
        planned = await factory_type(**kwargs)._runtime_for(context)
        routes = []
        def direct(child):
            routes.append((child._gateway, child._logical_model, (), ()))
        def dispatch(child):
            for agent in child._plan.agents:
                routes.append((child._gateway, agent.logical_model, agent.allowed_tools, agent.fallback_models))
            assert child._plan.max_parallelism <= defaults._logical_model_capacity(platform, 'deepseek')
        def discuss(child):
            routes.append((child._gateway, child._plan.selector_model, (), ()))
            for participant in child._plan.participants:
                routes.append((child._gateway, participant.logical_model, participant.allowed_tools, ()))
        if mode is TaskMode.DIRECT:
            direct(planned)
        elif mode is TaskMode.DISPATCH:
            dispatch(planned._child)
        elif mode is TaskMode.DISCUSS:
            discuss(planned._child)
        else:
            dispatch(planned._child._dispatch)
            discuss(planned._child._discussion)
            direct(planned._child._synthesizer)
        assert routes and all(rows for rows in seen)
        assert {row.id for rows in seen for row in rows} == {'deepseek_1'}
        assert set(defaults._logical_model_capacities(platform)) == {'deepseek'}
        if mode is not TaskMode.DIRECT:
            assert any(tools for _, _, tools, _ in routes)
            assert any(not tools for _, _, tools, _ in routes)
        for gateway, alias, tools, fallbacks in routes:
            assert alias == 'deepseek' and not fallbacks
            assert not gateway._fallbacks
            required = frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}
                                 if tools else {ModelCapability.TEXT})
            request = ModelRequest(logical_model=alias,
                messages=(ModelMessage(role='user', content='Read evidence'),),
                required_capabilities=required)
            assert gateway.context_window_tokens(request) == 100_000
            assert {row.id for row in gateway._registry.candidates(alias, required)} == {'deepseek_1'}


@pytest.mark.asyncio
async def test_original_second_deployment_id_constraint_and_capacity_survive_filtering():
    platform = config()
    original = platform.models['qwen'].deployments[0]
    rejected = original.model_copy(update={'model': 'qwen-disallowed', 'max_concurrency': 99})
    allowed = original.model_copy(update={'model': 'qwen-allowed', 'max_concurrency': 1})
    platform.models['qwen'].deployments = [rejected, allowed]
    constraint = DeploymentRoutingConstraint(logical_model='qwen', provider='qwen', model='qwen-allowed')
    routing = {'harness_decision': {'selected_logical_model': 'qwen',
                                  'selected_provider': 'qwen', 'selected_model': 'qwen-allowed'}}
    with review_scope(platform, ('qwen_2',)):
        assigned = defaults._assign_models_to_roles((role(tools=('workspace.read',)),), platform,
            default_model='qwen', task='Read evidence', deployment_constraint=constraint)
        assert assigned[0].model == 'qwen'
        assert defaults._logical_model_capacities(platform, deployment_constraint=constraint) == {'qwen': 1}
        gateway, _, _ = await defaults._gateway_for_config(platform, tenant_id=TENANT_ID,
            secret_service=FakeSecretService(), capacity_factory=_immediate_capacity,
            transport=FakeTransport(), routing_decision=routing)
        assert [row.id for row in gateway._registry.deployments] == ['qwen_2']
        assert gateway._registry.deployments[0].provider_model == 'qwen/qwen-allowed'
        with pytest.raises(defaults.HarnessModelSelectionError):
            defaults._assign_models_to_roles((role(),), platform, default_model='qwen',
                task='Read evidence', deployment_constraint=replace(constraint, model='qwen-disallowed'))


def test_tool_capability_must_exist_on_the_authorized_deployment():
    platform = config()
    original = platform.models['qwen'].deployments[0]
    platform.models['qwen'].deployments = [original, original.model_copy(update={'capabilities': ['text']})]
    with review_scope(platform, ('qwen_2',)):
        assert defaults._assign_models_to_roles((role(),), platform,
            default_model='qwen', task='Read evidence')[0].model == 'qwen'
        with pytest.raises(defaults.HarnessModelSelectionError):
            defaults._assign_models_to_roles((role(tools=('workspace.read',)),), platform,
                default_model='qwen', task='Read evidence')


def test_authorized_shared_quota_preserves_the_most_restrictive_capacity():
    platform = config()
    for alias, limit in (('deepseek', 8), ('qwen', 1)):
        platform.models[alias].deployments[0].quota_scope_id = 'shared'
        platform.models[alias].deployments[0].max_concurrency = limit
    platform.models['qwen'].deployments.append(
        platform.models['qwen'].deployments[0].model_copy(update={'max_concurrency': 99}))
    with review_scope(platform, ('deepseek_1', 'qwen_1')):
        assert defaults._logical_model_capacities(platform) == {'deepseek': 1, 'qwen': 1}
        assert defaults._model_scope_limits(platform) == {'deepseek': {'shared': 1}, 'qwen': {'shared': 1}}


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed_ids,route', [
    ((), {}),
    (('deepseek_1',), {'direct_model': 'qwen'}),
])
async def test_no_authorized_selected_model_fails_typed_before_capacity_or_transport(allowed_ids, route):
    platform = config()
    async def forbidden_capacity(*args):
        pytest.fail('An unavailable selected model must fail before capacity setup')
    transport = FakeTransport()
    with review_scope(platform, allowed_ids), pytest.raises(defaults.HarnessModelSelectionError):
        await defaults._gateway_for_config(platform, tenant_id=TENANT_ID,
            secret_service=FakeSecretService(), capacity_factory=forbidden_capacity,
            transport=transport, routing_decision=route)
    assert not transport.calls
