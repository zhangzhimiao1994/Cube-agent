
import httpx
import pytest

from agent_hub.openclaw.remote_adapter import (
    OpenClawRemoteAdapter,
    OpenClawRemoteAdapterError,
    probe_remote_openclaw_adapter,
)


@pytest.mark.asyncio
async def test_remote_adapter_probe_requires_matching_platform_and_kind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/openclaw/health"
        assert request.headers["Authorization"] == "Bearer adapter-token"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "platform": "windows",
                "capabilities": ["server_command", "file_read"],
            },
        )

    transport = httpx.MockTransport(handler)
    probe = await probe_remote_openclaw_adapter(
        OpenClawRemoteAdapter(
            platform="windows",
            target_type="desktop",
            target="local-windows-pc",
            base_url="http://adapter.local",
        ),
        bearer_token="adapter-token",
        required_kind="file_read",
        transport=transport,
    )

    assert probe.status == "available"
    assert probe.platform == "windows"
    assert probe.capabilities == ("server_command", "file_read")

    with pytest.raises(OpenClawRemoteAdapterError, match="does not support desktop_action"):
        await probe_remote_openclaw_adapter(
            OpenClawRemoteAdapter(
                platform="windows",
                target_type="desktop",
                target="local-windows-pc",
                base_url="http://adapter.local",
            ),
            bearer_token="adapter-token",
            required_kind="desktop_action",
            transport=transport,
        )


@pytest.mark.asyncio
async def test_remote_adapter_probe_rejects_platform_mismatch() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"status": "ok", "platform": "linux", "capabilities": ["server_command"]},
        )
    )

    with pytest.raises(OpenClawRemoteAdapterError, match="platform mismatch"):
        await probe_remote_openclaw_adapter(
            OpenClawRemoteAdapter(
                platform="windows",
                target_type="server",
                target="local-windows-pc",
                base_url="http://adapter.local",
            ),
            bearer_token="adapter-token",
            required_kind="server_command",
            transport=transport,
        )
