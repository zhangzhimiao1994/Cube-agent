from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

from agent_hub.mcp.client import StdioMcpClient
from agent_hub.mcp.types import McpServerDefinition, McpTransportKind

TENANT_ID = UUID("66666666-6666-4666-8666-666666666666")


async def test_stdio_client_invokes_real_mcp_server_process(tmp_path: Path) -> None:
    server_script = tmp_path / "stdio_mcp_server.py"
    server_script.write_text(
        """
import json
import sys


def read_message():
    header = sys.stdin.buffer.readline()
    if not header:
        return None
    if not header.lower().startswith(b"content-length:"):
        raise SystemExit(2)
    length = int(header.decode("ascii").split(":", 1)[1].strip())
    while True:
        line = sys.stdin.buffer.readline()
        if line in {b"\\r\\n", b"\\n", b""}:
            break
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def write_message(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\\r\\n\\r\\n".encode("ascii") + raw)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "smoke-mcp", "version": "1.0.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo payload",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    elif method == "tools/call":
        result = {
            "content": [
                {
                    "type": "text",
                    "text": "echo:" + message["params"]["arguments"]["text"],
                }
            ]
        }
    else:
        result = {}
    write_message({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
""",
        encoding="utf-8",
    )
    server = McpServerDefinition(
        tenant_id=TENANT_ID,
        id="local",
        transport=McpTransportKind.STDIO,
        command=sys.executable,
        args=(str(server_script),),
        executable_allowlist=(sys.executable,),
        timeout_seconds=5,
    )
    client = StdioMcpClient()

    tools = await client.discover_tools(server)
    result = await client.invoke(server, "echo", {"text": "hello"})

    assert [tool.name for tool in tools] == ["echo"]
    assert result.content == {"content": [{"type": "text", "text": "echo:hello"}]}
