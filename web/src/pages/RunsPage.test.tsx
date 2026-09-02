import { describe, expect, it } from "vitest";

import type { RunDetail } from "../api/client";
import { runConversationId, runDetailVersion } from "./RunsPage";

const baseRun: RunDetail = {
  id: "22222222-2222-4222-8222-222222222222",
  status: "running",
  mode: "dispatch",
  version: 9,
  conversation_id: "conv-top-level",
  request: "test",
  created_at: "2026-09-02T00:00:00Z",
  queue_wait_ms: 0,
  capacity_wait_ms: 0,
  cost_usd: "0.00",
  events: [],
  artifacts: [],
  explicit_details: {},
  failure_diagnostics: [],
  tool_lifecycle: [],
};

describe("runConversationId", () => {
  it("falls back to the top-level conversation id when explicit details omit it", () => {
    expect(runConversationId(baseRun)).toBe("conv-top-level");
  });

  it("keeps explicit detail conversation id authoritative", () => {
    expect(
      runConversationId({
        ...baseRun,
        explicit_details: { conversation_id: "conv-explicit" },
      }),
    ).toBe("conv-explicit");
  });
});

describe("runDetailVersion", () => {
  it("prefers the top-level run version", () => {
    expect(
      runDetailVersion({
        ...baseRun,
        version: 9,
        explicit_details: { version: "3" },
      }),
    ).toBe(9);
  });

  it("falls back to explicit detail version for rolling upgrades", () => {
    expect(
      runDetailVersion({
        ...baseRun,
        version: 0,
        explicit_details: { version: "3" },
      }),
    ).toBe(3);
  });
});
