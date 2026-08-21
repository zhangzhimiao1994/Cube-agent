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


def test_feishu_openapi_sender_splits_long_direct_text_without_truncation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path == "/open-apis/im/v1/messages/om_long/reply":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 404})

    sender = FeishuOpenAPIReplySender(
        api_base="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )
    text = "\n".join(f"第 {index} 条完整结论" for index in range(900))

    asyncio.run(
        sender.reply_text(
            settings=FeishuSettings.model_validate({"app_id": "app", "app_secret": "secret"}),
            message_id="om_long",
            text=text,
        )
    )

    reply_requests = [request for request in requests if request.url.path.endswith("/reply")]
    assert len(reply_requests) > 1
    combined = "\n".join(json.loads(json.loads(request.content)["content"])["text"] for request in reply_requests)
    assert "第 0 条完整结论" in combined
    assert "第 899 条完整结论" in combined
    assert "已截断" not in combined
    assert all(len(json.loads(json.loads(request.content)["content"])["text"]) <= 3800 for request in reply_requests)


def test_feishu_openapi_sender_keeps_long_markdown_table_as_one_rich_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path == "/open-apis/im/v1/messages/om_long_table/reply":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 404})

    sender = FeishuOpenAPIReplySender(
        api_base="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )
    rows = "\n".join(f"| 第 {index} 行 | 能做 {index} | 不能做 {index} |" for index in range(360))
    table = "我能做的 VS 我不能做的\n\n| -- | 能做 | 不能做 |\n| --- | --- | --- |\n" + rows

    asyncio.run(
        sender.reply_text(
            settings=FeishuSettings.model_validate({"app_id": "app", "app_secret": "secret"}),
            message_id="om_long_table",
            text=table,
        )
    )

    reply_requests = [request for request in requests if request.url.path.endswith("/reply")]
    assert len(reply_requests) == 1
    payload = json.loads(reply_requests[0].content)
    assert payload["msg_type"] == "post"
    content = json.loads(payload["content"])
    rendered = content["post"]["zh_cn"]["content"][0][0]["text"]
    assert "| -- | 能做 | 不能做 |" in rendered
    assert "| --- | --- | --- |" in rendered
    assert "第 0 行" in rendered
    assert "第 359 行" in rendered
    assert "已截断" not in rendered
    assert len(rendered) > 3800


def test_feishu_openapi_sender_truncates_oversized_markdown_table_on_row_boundary() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path == "/open-apis/im/v1/messages/om_huge_table/reply":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 404})

    sender = FeishuOpenAPIReplySender(
        api_base="https://open.feishu.cn/open-apis",
        transport=httpx.MockTransport(handler),
    )
    rows = "\n".join(
        f"| 第 {index:03d} 行 | {'x' * 120} | {'y' * 80} |" for index in range(300)
    )
    table = "超长表格：\n\n| 行 | 说明 | 备注 |\n| --- | --- | --- |\n" + rows

    asyncio.run(
        sender.reply_text(
            settings=FeishuSettings.model_validate({"app_id": "app", "app_secret": "secret"}),
            message_id="om_huge_table",
            text=table,
        )
    )

    reply_requests = [request for request in requests if request.url.path.endswith("/reply")]
    assert len(reply_requests) == 1
    payload = json.loads(reply_requests[0].content)
    assert payload["msg_type"] == "post"
    content = json.loads(payload["content"])
    rendered = content["post"]["zh_cn"]["content"][0][0]["text"]
    table_part, notice = rendered.split("\n\n……", 1)
    table_lines = [line for line in table_part.splitlines() if line.startswith("|")]
    assert table_lines[-1].endswith(" |")
    assert "内容较长" in notice
