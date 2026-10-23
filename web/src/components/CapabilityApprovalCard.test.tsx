import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilityApprovalCard } from "./CapabilityApprovalCard";

const authorization = vi.hoisted(() => ({ permissions: ["run:read", "run:create"], signedIn: true }));
vi.mock("../auth/AuthProvider", () => ({
  useAuth: () => ({
    user: authorization.signedIn ? { tenant_id: "tenant", user_id: "actor", role: "operator" } : null,
    hasPermission: (permission: string) => authorization.permissions.includes(permission),
  }),
}));

afterEach(() => {
  cleanup();
  authorization.permissions = ["run:read", "run:create"];
  authorization.signedIn = true;
});

function props() {
  return {
    approval: { approvalId: "approval-A", toolName: "workspace.write", summary: "Synthetic action" },
    sessionAvailable: true,
    onApprove: vi.fn(), onApproveConversation: vi.fn(), onReject: vi.fn(),
  };
}

function expectNoDecision(value: ReturnType<typeof props>) {
  expect(value.onApprove).not.toHaveBeenCalled();
  expect(value.onApproveConversation).not.toHaveBeenCalled();
  expect(value.onReject).not.toHaveBeenCalled();
}

describe("capability approval decision permissions", () => {
  it.each(["批准", "本次会话批准", "拒绝"])("never invokes %s for a read-only actor", (label) => {
    authorization.permissions = ["run:read"];
    const value = props();
    render(<CapabilityApprovalCard {...value} />);
    const button = screen.getByRole<HTMLButtonElement>("button", { name: label });
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expectNoDecision(value);
    expect(screen.getByText("当前身份没有操作审批权限")).not.toBeNull();
  });

  it("disables every decision immediately when the same actor loses run:create", () => {
    const value = props();
    const { rerender } = render(<CapabilityApprovalCard {...value} />);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "批准" }).disabled).toBe(false);
    authorization.permissions = ["run:read"];
    rerender(<CapabilityApprovalCard {...value} />);
    for (const button of screen.getAllByRole<HTMLButtonElement>("button")) {
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
    }
    expectNoDecision(value);
  });

  it("fails closed without a current authenticated actor", () => {
    authorization.signedIn = false;
    const value = props();
    render(<CapabilityApprovalCard {...value} />);
    for (const button of screen.getAllByRole<HTMLButtonElement>("button")) {
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
    }
    expectNoDecision(value);
  });

  it("preserves all three authorized actions when session readiness is current", () => {
    const value = props();
    render(<CapabilityApprovalCard {...value} />);
    for (const button of screen.getAllByRole<HTMLButtonElement>("button")) {
      expect(button.disabled).toBe(false);
      fireEvent.click(button);
    }
    expect(value.onApprove).toHaveBeenCalledTimes(1);
    expect(value.onApproveConversation).toHaveBeenCalledTimes(1);
    expect(value.onReject).toHaveBeenCalledTimes(1);
  });

  it("never lets run:create override stale or unsupported session readiness", () => {
    const value = props();
    render(<CapabilityApprovalCard {...value} sessionAvailable={false}
      sessionUnavailableReason="Stale approval version" />);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "批准" }).disabled).toBe(false);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "拒绝" }).disabled).toBe(false);
    const session = screen.getByRole<HTMLButtonElement>("button", { name: "本次会话批准" });
    expect(session.disabled).toBe(true);
    fireEvent.click(session);
    expectNoDecision(value);
    expect(screen.getByText("Stale approval version")).not.toBeNull();
  });

  it.each(["approve", "approve_session", "reject"])("keeps all decisions locked while %s is pending", (decision) => {
    const value = props();
    render(<CapabilityApprovalCard {...value} decision={{ status: "pending", decision }} />);
    for (const button of screen.getAllByRole<HTMLButtonElement>("button")) {
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
    }
    expectNoDecision(value);
  });
});
