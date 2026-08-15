from agent_hub.models.profiles import infer_model_traits


def test_infers_mainstream_multimodal_model_traits_from_names() -> None:
    traits = infer_model_traits(
        logical_model="gemini_pro",
        deployments=(
            ("google", "gemini-2.5-pro", {"text"}),
        ),
    )

    assert {"vision", "multimodal", "reasoning", "analysis", "structured"}.issubset(traits)


def test_infers_ordinary_language_model_traits_from_mainstream_names() -> None:
    traits = infer_model_traits(
        logical_model="grok_fast",
        deployments=(
            ("xai", "grok-4-fast", {"text"}),
        ),
    )

    assert {"general", "reasoning", "realtime", "fast"}.issubset(traits)


def test_declared_capabilities_still_contribute_hard_trait_aliases() -> None:
    traits = infer_model_traits(
        logical_model="custom_review_model",
        deployments=(
            ("custom", "private-model", {"text", "tool_calling", "structured_output"}),
        ),
    )

    assert {"text", "tool", "tool_calling", "structured", "structured_output"}.issubset(traits)