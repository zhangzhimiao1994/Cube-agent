from __future__ import annotations

import asyncio
import json

import httpx

from agent_hub.channels.feishu.reply import FeishuOpenAPIReplySender
from agent_hub.channels.feishu.settings import FeishuSettings


def test_feishu_openapi_sender_posts_table_reply_as_rich_markdown() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path == "/open-apis/im/v1/messages/om_table/reply":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 404})

    sender = FeishuOpenAPIReplySender(
        api_base="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        sender.reply_text(
            settings=FeishuSettings.model_validate({"app_id": "app", "app_secret": "secret"}),
            message_id="om_table",
            text=(
                "对比如下：\n\n"
                "| 类型 | 能做 | 不能做 |\n"
                "| --- | --- | --- |\n"
                "| 个股 | 深度分析 | 主动推荐 |"
            ),
        )
    )

    reply_request = requests[1]
    assert reply_request.headers["authorization"] == "Bearer tenant-token"
    payload = json.loads(reply_request.content)
    assert payload["msg_type"] == "post"
    content = json.loads(payload["content"])
    assert content["post"]["zh_cn"]["content"][0][0]["tag"] == "md"
    assert "| 类型 | 能做 | 不能做 |" in content["post"]["zh_cn"]["content"][0][0]["text"]
