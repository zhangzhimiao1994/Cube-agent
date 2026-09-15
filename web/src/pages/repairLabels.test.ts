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
});
