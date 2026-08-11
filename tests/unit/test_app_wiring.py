from pathlib import Path

from fastapi.responses import FileResponse

from agent_hub.app import _web_ui_response, create_app


def test_web_ui_rejects_sibling_path_with_shared_prefix(tmp_path: Path) -> None:
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("index", encoding="utf-8")
    sibling = tmp_path / "web-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")

    response = _web_ui_response(web_root, "../web-evil/secret.txt")

    assert isinstance(response, FileResponse)
    assert Path(response.path) == web_root / "index.html"


def test_create_app_mounts_feishu_webhook_on_main_api() -> None:
    application = create_app(
        auth_service=object(),
        rate_limiter=object(),
        config_service=object(),
        run_service=object(),
    )

    paths = {getattr(route, "path", "") for route in application.routes}

    assert "/channels/feishu/events" in paths
    assert application.state.channel_runtime_config is None
