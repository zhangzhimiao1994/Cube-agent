import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type ExecutionBackend } from "../api/client";
import { ExecutionEnvironmentsPage } from "./ExecutionEnvironmentsPage";

const healthy: ExecutionBackend = {
  id: "systemd", name: "本机 systemd 隔离", adapter: "SystemdSkillSandbox",
  description: "本地隔离", isolation: "固定隔离", cost: "本机资源",
  available: true, reason: null, supported_sandbox_profiles: ["workspace_write"],
};
const pending: ExecutionBackend = { ...healthy, available: false, reason: "systemd_probe_pending" };
const clients: QueryClient[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(() => { throw new Error("unexpected network"); }));
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function tick(milliseconds = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
}

async function mount(copies = 1) {
  const client = new QueryClient({ defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false },
  } });
  clients.push(client);
  const view = render(<QueryClientProvider client={client}>
    <ExecutionEnvironmentsPage />
    {copies === 2 ? <ExecutionEnvironmentsPage /> : null}
  </QueryClientProvider>);
  await tick();
  await tick();
  return view;
}

describe("execution backend pending discovery", () => {
  it("shows preparing and recovers without manual refresh, then stops polling", async () => {
    const read = vi.spyOn(api, "executionBackends").mockResolvedValueOnce([pending]).mockResolvedValue([healthy]);
    await mount();
    expect(screen.queryByText("准备中")).not.toBeNull();
    expect(screen.queryByText(/不可用：systemd_probe_pending/)).toBeNull();
    await tick(3000);
    expect(screen.queryByText("可用")).not.toBeNull();
    expect(read).toHaveBeenCalledTimes(2);
    await tick(10000);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("bounds pending polling and still allows the existing manual recheck", async () => {
    const read = vi.spyOn(api, "executionBackends").mockResolvedValue([pending]);
    await mount();
    await tick(70000);
    const calls = read.mock.calls.length;
    expect(calls).toBeGreaterThan(1);
    expect(calls).toBeLessThanOrEqual(31);
    await tick(70000);
    expect(read).toHaveBeenCalledTimes(calls);
    read.mockResolvedValue([healthy]);
    fireEvent.click(screen.getByRole("button", { name: "重新探测" }));
    await tick();
    await tick();
    expect(screen.queryByText("可用")).not.toBeNull();
    expect(read).toHaveBeenCalledTimes(calls + 1);
  });

  it.each(["systemd_transient_unit_unavailable", "systemd_wrong_probe_pending"])(
    "does not poll a hard or unknown reason %s", async (reason) => {
      const read = vi.spyOn(api, "executionBackends").mockResolvedValue([{ ...pending, reason }]);
      await mount();
      await tick(70000);
      expect(read).toHaveBeenCalledTimes(1);
      expect(screen.queryByText("准备中")).toBeNull();
    },
  );

  it("stops on an HTTP failure without continuing to poll stale pending data", async () => {
    const read = vi.spyOn(api, "executionBackends").mockResolvedValueOnce([pending])
      .mockRejectedValue(new Error("unavailable"));
    await mount();
    await tick(4000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeNull();
    await tick(70000);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("shares the query between consumers and stops when unmounted", async () => {
    const read = vi.spyOn(api, "executionBackends").mockResolvedValueOnce([pending]).mockResolvedValue([healthy]);
    const view = await mount(2);
    expect(read).toHaveBeenCalledTimes(1);
    await tick(3000);
    expect(read).toHaveBeenCalledTimes(2);
    view.unmount();
    await tick(10000);
    expect(read).toHaveBeenCalledTimes(2);
  });
});
