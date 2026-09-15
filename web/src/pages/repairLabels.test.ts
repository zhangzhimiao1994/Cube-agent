import { describe, expect, it } from "vitest";

import { repairErrorCodeLabel } from "./repairLabels";

describe("repair error-code labels", () => {
  it("localizes standard plugin failure codes", () => {
    expect(repairErrorCodeLabel("plugin.adapter_unavailable")).toBe("插件适配器不可用");
    expect(repairErrorCodeLabel("plugin.timeout")).toBe("插件执行超时");
    expect(repairErrorCodeLabel("plugin.credential_unavailable")).toBe("插件凭据不可用");
    expect(repairErrorCodeLabel("plugin.invalid_arguments")).toBe("插件参数无效");
    expect(repairErrorCodeLabel("plugin.invalid_result")).toBe("插件结果契约无效");
    expect(repairErrorCodeLabel("plugin.backend_unavailable")).toBe("插件后端不可用");
    expect(repairErrorCodeLabel("plugin.endpoint_unavailable")).toBe("插件端点不可用");
    expect(repairErrorCodeLabel("plugin.sandbox_unsupported")).toBe("插件沙箱不支持");
  });

  it("localizes standard MCP failure codes", () => {
    expect(repairErrorCodeLabel("mcp.tool_unavailable")).toBe("MCP 工具不可用");
    expect(repairErrorCodeLabel("mcp.timeout")).toBe("MCP 调用超时");
    expect(repairErrorCodeLabel("mcp.server_not_discovered")).toBe("MCP 服务未发现");
    expect(repairErrorCodeLabel("mcp.server_timeout")).toBe("MCP 服务超时");
    expect(repairErrorCodeLabel("mcp.server_failed")).toBe("MCP 服务失败");
    expect(repairErrorCodeLabel("mcp.server_unavailable")).toBe("MCP 服务不可用");
  });
});
