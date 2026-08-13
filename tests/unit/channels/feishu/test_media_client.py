from __future__ import annotations

import httpx
from pydantic import SecretStr

from agent_hub.channels.feishu.media import FeishuOpenAPIMediaClient
from agent_hub.channels.feishu.settings import FeishuSettings


async def test_openapi_media_client_downloads_message_resource() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert request.method == "POST"
            assert request.content == b'{"app_id":"cli_test","app_secret":"secret"}'
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-token"})
        if request.url.path == "/open-apis/im/v1/messages/om_1/resources/img_1":
            assert request.method == "GET"
            assert request.url.params["type"] == "image"
            assert request.headers["authorization"] == "Bearer t-token"
            return httpx.Response(200, content=b"image-bytes")
        return httpx.Response(404)

    client = FeishuOpenAPIMediaClient(
        settings=FeishuSettings(app_id="cli_test", app_secret=SecretStr("secret")),
        transport=httpx.MockTransport(handler),
    )

    token = await client.tenant_access_token("tenant_1")
    chunks = [
        chunk
        async for chunk in client.download_resource(
            message_id="om_1",
            resource_key="img_1",
            tenant_access_token=token,
        )
    ]

    assert token == "t-token"
    assert b"".join(chunks) == b"image-bytes"
    assert [request.url.path for request in requests] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/im/v1/messages/om_1/resources/img_1",
    ]
