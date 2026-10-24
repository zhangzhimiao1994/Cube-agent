"""Keep GitHub Actions opt-in while hosted CI quota is unavailable."""

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
WORKFLOWS = sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=[path.name for path in WORKFLOWS])
def test_workflows_only_allow_manual_dispatch_and_harness_stage_tags(workflow_path: Path) -> None:
    # BaseLoader preserves GitHub's "on" key instead of treating it as a YAML 1.1 boolean.
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert isinstance(workflow, dict)
    triggers = workflow.get("on")
    assert isinstance(triggers, dict), f"{workflow_path.name} must declare manual dispatch"
    assert set(triggers) == {"workflow_dispatch", "push"}, (
        f"{workflow_path.name} must allow manual dispatch and stage tags; found {sorted(triggers)}"
    )
    assert triggers["push"] == {"tags": ["harness-stage-*"]}, (
        f"{workflow_path.name} must restrict push to harness-stage-* tags"
    )
