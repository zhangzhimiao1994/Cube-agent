from __future__ import annotations

from _pytest.monkeypatch import MonkeyPatch

from agent_hub.channels.feishu.settings import FeishuSettings


def test_blank_feishu_environment_values_are_ignored(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "")

    settings = FeishuSettings()

    assert settings.app_id == "cli_a_test"
    assert settings.app_secret_value() == "development-only"
    assert settings.verification_token_value() == "test-token"
    assert settings.encrypt_key_value() == "test-encrypt-key"
