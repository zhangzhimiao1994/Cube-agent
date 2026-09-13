from agent_hub.runtime.project_preflight_context import project_preflight_context_text


def test_project_preflight_context_formats_approved_guidance() -> None:
    text = project_preflight_context_text(
        {
            "project_preflight_approved": True,
            "project_preflight_proposal": {
                "kind": "project_architecture_preflight",
                "title": "超大型项目架构预检",
                "capability": "project.preflight_architecture",
                "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
                "graph_path": "architecture-map.html",
                "requires_constraints_and_skills_reading": True,
                "summary": "先读取项目约束和技能规则，再生成计划和图谱。",
            },
        }
    )

    assert "PROJECT_PREFLIGHT_CONTEXT" in text
    assert "project.preflight_architecture" in text
    assert "PROJECT_ARCHITECTURE_PLAN.md" in text
    assert "architecture-map.html" in text
    assert "constraints and skill rules" in text
    assert "staged implementation" in text
    assert "stage_status" in text
    assert "verification_evidence" in text
    assert "remaining_risks" in text
    assert "acceptance_review" in text


def test_project_preflight_context_ignores_unapproved_payloads() -> None:
    assert project_preflight_context_text({}) == ""
    assert (
        project_preflight_context_text(
            {
                "project_preflight_proposal": {
                    "kind": "project_architecture_preflight",
                    "capability": "project.preflight_architecture",
                }
            }
        )
        == ""
    )
