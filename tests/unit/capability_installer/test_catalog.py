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
    assert plan.plugin_request["endpoint_url"] == "https://plugins.example/office-doc-search/invoke"
    capabilities = plan.plugin_request["capabilities"]
    assert isinstance(capabilities, list)
    assert isinstance(capabilities[0], dict)
    assert capabilities[0]["policy_effect"] == "require_approval"
    assert "credential_ref" not in plan.plugin_request
