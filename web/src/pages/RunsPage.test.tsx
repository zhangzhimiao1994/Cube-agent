import { describe, expect, it } from "vitest";

import type { RunDetail } from "../api/client";
import { conversationMessages, mergeConversationRuns, runConversationId, runDetailVersion, runProcessItems } from "./RunsPage";

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

describe("conversation ordering", () => {
  it("renders conversation turns by run creation time even when incoming data is out of order", () => {
    const laterRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "第二轮请求",
      created_at: "2026-09-02T00:02:00Z",
    };
    const earlierRun: RunDetail = {
      ...baseRun,
      request: "第一轮请求",
      created_at: "2026-09-02T00:01:00Z",
    };

    const messages = conversationMessages([laterRun, earlierRun]);

    expect(messages.filter((message) => message.id.endsWith("-request")).map((message) => message.body)).toEqual([
      "第一轮请求",
      "第二轮请求",
    ]);
  });

  it("keeps merged cached conversation runs in chronological order", () => {
    const laterRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "第二轮请求",
      created_at: "2026-09-02T00:02:00Z",
    };
    const earlierRun: RunDetail = {
      ...baseRun,
      request: "第一轮请求",
      created_at: "2026-09-02T00:01:00Z",
    };

    const merged = mergeConversationRuns([laterRun], [earlierRun, laterRun]);

    expect(merged.map((run) => run.request)).toEqual(["第一轮请求", "第二轮请求"]);
  });

  it("prefers fresh conversation snapshots over stale cached runs with the same id", () => {
    const cachedRun: RunDetail = {
      ...baseRun,
      request: "旧请求",
      artifacts: [{ id: "artifact-old", kind: "markdown", title: "旧产物", text: "旧回复" }],
    };
    const freshRun: RunDetail = {
      ...baseRun,
      request: "新请求",
      artifacts: [{ id: "artifact-new", kind: "markdown", title: "新产物", text: "新回复" }],
    };

    const merged = mergeConversationRuns([cachedRun], [freshRun]);

    expect(merged).toHaveLength(1);
    expect(merged[0].request).toBe("新请求");
    expect(merged[0].artifacts[0]?.text).toBe("新回复");
  });

  it("does not downgrade cached conversation runs with older incoming progress", () => {
    const cachedRun: RunDetail = {
      ...baseRun,
      version: 2,
      request: "更新后的请求",
      events: [
        {
          sequence: 2,
          kind: "step.completed",
          message: "step.completed",
          created_at: "2026-09-02T00:00:02Z",
          participants: [],
          payload: {},
        },
      ],
    };
    const staleIncomingRun: RunDetail = {
      ...baseRun,
      version: 1,
      request: "旧请求",
      events: [],
    };

    const merged = mergeConversationRuns([cachedRun], [staleIncomingRun]);

    expect(merged).toHaveLength(1);
    expect(merged[0].request).toBe("更新后的请求");
    expect(merged[0].version).toBe(2);
  });

  it("keeps runs without reliable timestamps after timestamped history", () => {
    const timestampedRun: RunDetail = {
      ...baseRun,
      request: "已有历史",
      created_at: "2026-09-02T00:01:00Z",
    };
    const pendingRun: RunDetail = {
      ...baseRun,
      id: "33333333-3333-4333-8333-333333333333",
      request: "新提交待写入时间",
      created_at: null,
    };

    const messages = conversationMessages([pendingRun, timestampedRun]);

    expect(messages.filter((message) => message.id.endsWith("-request")).map((message) => message.body)).toEqual([
      "已有历史",
      "新提交待写入时间",
    ]);
  });

  it("orders process cards by event sequence when backend events arrive out of order", () => {
    const outOfOrderRun: RunDetail = {
      ...baseRun,
      events: [
        {
          sequence: 2,
          kind: "artifact.created",
          message: "artifact.created",
          created_at: "2026-09-02T00:00:02Z",
          actor: "engineer",
          participants: [],
          tool_name: "artifact_writer",
          step_id: "write-output",
          action: null,
          decision: null,
          payload: { result: "第二步产物" },
        },
        {
          sequence: 1,
          kind: "step.started",
          message: "step.started",
          created_at: "2026-09-02T00:00:01Z",
          actor: "main_agent",
          participants: [],
          tool_name: null,
          step_id: "plan",
          action: null,
          decision: null,
          payload: { task: "第一步规划" },
        },
      ],
    };

    const items = runProcessItems(outOfOrderRun, new Map());

    expect(items.flatMap((item) => (item.createdAt ? [item.createdAt] : []))).toEqual([
      "2026-09-02T00:00:01Z",
      "2026-09-02T00:00:02Z",
    ]);
  });
});
