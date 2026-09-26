from agent_hub.capability_installer.catalog import (
    TrustedCapabilityCatalog,
    default_trusted_capability_entries,
)


def test_catalog_resolves_chinese_office_document_search_to_installable_entry() -> None:
    catalog = TrustedCapabilityCatalog(default_trusted_capability_entries())

    matches = catalog.resolve("需要读取 Office 文档并进行搜索查询")

    assert matches
    entry = matches[0]
    assert entry.id == "office_doc_search"
    assert entry.name_cn == "Office 文档搜索"
    assert entry.plugin.id == "office-doc-search"
    assert entry.plugin.capabilities[0].id == "office.search_documents"
    assert entry.risks == ("read_only",)


def test_catalog_builds_deterministic_install_plan_with_safe_plugin_payload() -> None:
    catalog = TrustedCapabilityCatalog(default_trusted_capability_entries())
    entry = catalog.get("office_doc_search")

    plan = catalog.plan(entry.id, query="搜索 docx xlsx pptx")

    assert plan.id == catalog.plan(entry.id, query="搜索 docx xlsx pptx").id
    assert plan.status == "planned"
    assert plan.entry_id == entry.id
    assert plan.requires_confirmation is True
    assert plan.rollback_strategy == "restore_previous_plugin_or_delete_installed_plugin"
    assert plan.plugin_id == "office-doc-search"
    assert plan.capabilities == ("office.search_documents",)
    assert plan.permission_summary == (
        "读取 Office 文档索引",
        "执行前仍按能力策略审批",
    )
    assert "plugins.example" not in str(plan.plugin_request)
    capabilities = plan.plugin_request["capabilities"]
    assert isinstance(capabilities, list)
    assert isinstance(capabilities[0], dict)
    assert capabilities[0]["policy_effect"] == "require_approval"
    assert "credential_ref" not in plan.plugin_request


def test_catalog_resolves_strix_security_testing_to_real_local_command_adapter() -> None:
    catalog = TrustedCapabilityCatalog(default_trusted_capability_entries())

    matches = catalog.resolve("需要 strix 自动化渗透能力")

    assert matches
    entry = matches[0]
    assert entry.id == "security_testing"
    assert entry.plugin.id == "security-testing"
    assert entry.plugin.endpoint_url is None
    assert entry.plugin.domain_allowlist == []
    assert entry.plugin.resource_config["command"] == "strix"
    assert entry.plugin.resource_config["base_args"] == ("--non-interactive",)
    assert entry.plugin.resource_config["required_commands"] == ("docker",)
    required_env = entry.plugin.resource_config["required_env_any"]
    allowed_extra_args = entry.plugin.capabilities[0].capability_config["allowed_extra_args"]
    assert isinstance(required_env, tuple)
    assert isinstance(allowed_extra_args, tuple)
    assert "OPENAI_API_KEY" in required_env
    assert "--max-turns" in allowed_extra_args
    assert entry.plugin.capabilities[0].adapter == "local_command"
    assert entry.plugin.capabilities[0].sandbox_profile == "local_process"
    assert entry.plugin.capabilities[0].capability_config["argument_style"] == "strix_assessment"


def test_default_capability_catalog_does_not_ship_placeholder_plugin_endpoints() -> None:
    for entry in default_trusted_capability_entries():
        payload = entry.plugin.model_dump(mode="json", exclude_none=True)

        assert "plugins.example" not in str(payload)
