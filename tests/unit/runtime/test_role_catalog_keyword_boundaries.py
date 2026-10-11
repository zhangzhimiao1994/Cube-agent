import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.project_scale import ProjectScaleMatrix, _capability_message
from agent_hub.models.gateway import ModelGateway
from agent_hub.runtime.defaults import _role_planning_task, _task_profile, _task_profiles
from agent_hub.runtime.role_catalog import RoleDefinition, default_role_catalog
from agent_hub.runtime.role_planner import RolePlanner, RolePlanningRequest, TaskProfile


@pytest.fixture(autouse=True)
def forbid_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_model_call(*args: object, **kwargs: object) -> None:
        pytest.fail("Role planning must not call a model")

    for method in ("complete", "complete_with_context", "stream_openai_compatible_events"):
        monkeypatch.setattr(ModelGateway, method, unexpected_model_call)


@pytest.fixture
def security_planner() -> RolePlanner:
    catalog = default_role_catalog().with_role(
        RoleDefinition(
            id="custom_sentinel",
            role="Custom Sentinel",
            purpose="expertise",
            mission="Review project risks.",
            must_answer=("What risk exists?",),
            allowed_tools=("read_context",),
            forbidden_actions=("do not execute external operations",),
            skills=("security",),
            output_schema={"summary": "string", "risks": "string[]"},
            modes=frozenset({"dispatch"}),
            profiles=frozenset({"general"}),
        )
    )
    return RolePlanner(role_catalog=catalog)


@pytest.mark.parametrize("directory_word", ("directory", "directories"))
def test_directory_does_not_select_director_for_general_dispatch(directory_word: str) -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task=f"Build a software project with persistent data {directory_word}.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
        )
    )

    assert "director" not in {role.id for role in plan.roles}


def test_insecurity_does_not_select_custom_security_skill(security_planner: RolePlanner) -> None:
    plan = security_planner.plan(
        RolePlanningRequest(
            task="Explain insecurity in a small team.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
        )
    )

    assert "custom_sentinel" not in {role.id for role in plan.roles}


@pytest.mark.parametrize(
    "task",
    (
        "director security",
        "lead-director security-review",
        "lead_director security_review",
        "(director); [security].",
        "DIRECTOR SeCuRiTy",
    ),
    ids=("exact", "hyphen", "underscore", "punctuation", "case"),
)
def test_real_role_words_and_custom_skills_remain_discoverable(
    security_planner: RolePlanner, task: str
) -> None:
    plan = security_planner.plan(
        RolePlanningRequest(task=task, mode=TaskMode.DISPATCH, profile=TaskProfile.GENERAL)
    )

    role_ids = {role.id for role in plan.roles}
    assert "director" in role_ids
    assert "custom_sentinel" in role_ids


def test_custom_role_name_remains_discoverable(security_planner: RolePlanner) -> None:
    plan = security_planner.plan(
        RolePlanningRequest(
            task="Ask the CUSTOM SENTINEL to review this proposal.",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
        )
    )

    assert "custom_sentinel" in {role.id for role in plan.roles}


def test_chinese_role_keywords_remain_substring_matches() -> None:
    plan = RolePlanner().plan(
        RolePlanningRequest(
            task="\u8bf7\u5bfc\u6f14\u5b89\u6392\u5de5\u4f5c",
            mode=TaskMode.DISPATCH,
            profile=TaskProfile.GENERAL,
        )
    )

    assert "director" in {role.id for role in plan.roles}


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
def test_real_capability_requests_do_not_select_director_for_directory(scale: str) -> None:
    case = next(
        case
        for case in ProjectScaleMatrix.default().cases
        if case.scale == scale and case.flow == "hybrid"
    )
    message = _capability_message(case)
    task = _role_planning_task(message)
    if scale in {"small", "medium"}:
        assert "directory" in task.casefold()

    plan = RolePlanner().plan(
        RolePlanningRequest(
            task=task,
            mode=TaskMode.DISPATCH,
            profile=_task_profile(message),
            profiles=_task_profiles(message),
        )
    )

    assert "director" not in {role.id for role in plan.roles}
