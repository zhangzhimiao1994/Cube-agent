"""Allow scoped quality pushes while deployment workflows stay opt-in."""

from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOWS = sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=[path.name for path in WORKFLOWS])
def test_only_quality_allows_push_besides_manual_dispatch(workflow_path: Path) -> None:
    # BaseLoader preserves GitHub's "on" key instead of treating it as a YAML 1.1 boolean.
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert isinstance(workflow, dict)
    triggers = workflow.get("on")
    assert isinstance(triggers, dict), f"{workflow_path.name} must declare manual dispatch"
    expected = {"push", "workflow_dispatch"} if workflow_path.name == "quality.yml" else {"workflow_dispatch"}
    assert set(triggers) == expected, (
        f"{workflow_path.name} must only allow {sorted(expected)}; found {sorted(triggers)}"
    )


def test_quality_push_only_targets_the_active_work_branch() -> None:
    workflow = yaml.load((WORKFLOW_DIR / "quality.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    push = workflow["on"].get("push")

    assert isinstance(push, dict), "quality must declare a scoped push trigger"
    assert set(push) == {"branches", "paths"}
    assert push["branches"] == ["fix/direct-hybrid-final-report-atomicity-oct29"]


@pytest.mark.parametrize(
    ("changed_path", "should_trigger"),
    [
        (".github/workflows/quality.yml", True),
        ("tests/unit/test_workflow_triggers.py", True),
        ("tests/compose.yml", True),
        ("src/agent_hub/runtime/direct.py", True),
        ("web/src/App.tsx", True),
        ("web/package-lock.json", True),
        ("scripts/lib/runtime.sh", True),
        ("deploy/native/install.sh", True),
        ("alembic/versions/0036_review_ledger.py", True),
        ("alembic.ini", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("install.sh", True),
        ("Dockerfile", True),
        (".dockerignore", True),
        (".gitattributes", True),
        ("README.md", False),
        ("README.zh-CN.md", False),
        ("docs/architecture.md", False),
        ("handoffs/current-state.md", False),
        (".superpowers/sdd/review-report.md", False),
    ],
)
def test_quality_push_path_filter_covers_checks_without_docs_only_runs(changed_path: str, should_trigger: bool) -> None:
    workflow = yaml.load((WORKFLOW_DIR / "quality.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    push = workflow["on"].get("push")

    assert isinstance(push, dict), "quality must declare a scoped push trigger"
    paths = push["paths"]
    assert isinstance(paths, list) and paths and all(isinstance(path, str) for path in paths)
    assert all(not path.startswith("!") for path in paths)
    assert any(fnmatchcase(changed_path, path) for path in paths) is should_trigger
