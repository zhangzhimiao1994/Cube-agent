from agent_hub.runs.repository import _question_search_excerpt


def test_question_search_excerpt_keeps_a_late_match_inside_a_bounded_result() -> None:
    question = f"{'前' * 900}唯一命中词{'后' * 900}"

    excerpt = _question_search_excerpt(question, "唯一命中词")

    assert "唯一命中词" in excerpt
    assert excerpt.startswith("...")
    assert excerpt.endswith("...")
    assert len(excerpt) <= 606


def test_question_search_excerpt_preserves_short_questions() -> None:
    assert _question_search_excerpt("短问题", "问题") == "短问题"
