import { afterEach, expect, it, vi } from "vitest";

import { api } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

it("retains plugin provenance without requiring it on built-in capabilities", async () => {
  const common = {
    adapter: "plugin_runtime", permission_class: "test.use", sandbox_profile: "local_process",
    policy_effect: "inherit", available: true, availability_reason: null, replay_safe: false,
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    schema_version: 1,
    capabilities: [
      { ...common, id: "sample.echo", kind: "plugin", plugin_id: "sample" },
      { ...common, id: "calculator.evaluate", kind: "builtin" },
    ],
  }), { status: 200, headers: { "Content-Type": "application/json" } })));
  const manifest = await api.capabilityManifest();
  expect(manifest.capabilities[0]).toHaveProperty("plugin_id", "sample");
  expect(manifest.capabilities[1]).not.toHaveProperty("plugin_id");
});
