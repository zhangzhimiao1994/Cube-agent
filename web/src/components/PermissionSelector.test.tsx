import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PermissionSelector, type PermissionMode } from "./PermissionSelector";

function Controlled({ autoReviewAvailable = true }: { autoReviewAvailable?: boolean }) {
  const [value, setValue] = useState<PermissionMode>("default");
  return <PermissionSelector value={value} onChange={setValue} autoReviewAvailable={autoReviewAvailable} />;
}

afterEach(() => vi.restoreAllMocks());

describe("PermissionSelector", () => {
  it("opens in a portal and selects a checked mode through the controlled callback", async () => {
    const user = userEvent.setup();
    const view = render(<Controlled />);
    const trigger = screen.getByRole("button", { name: /默认权限/ });
    await user.click(trigger);
    const menu = screen.getByRole("menu", { name: "沙箱权限" });
    expect(trigger.getAttribute("aria-label")).toContain("沙箱权限");
    expect(view.container.contains(menu)).toBe(false);
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(4);
    expect(screen.getByRole("menuitemradio", { name: /默认权限/ }).getAttribute("aria-checked")).toBe("true");
    await user.click(screen.getByRole("menuitemradio", { name: /只读/ }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("button", { name: /只读/ })).toBe(trigger);
    expect(document.activeElement).toBe(trigger);
    await user.click(trigger);
    expect(screen.getByRole("menuitemradio", { name: /只读/ }).getAttribute("aria-checked")).toBe("true");
  });

  it("does not emit changes for full access or unavailable rule review", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PermissionSelector value="default" onChange={onChange} autoReviewAvailable={false} />);
    await user.click(screen.getByRole("button", { name: /默认权限/ }));
    for (const name of [/自动审查/, /完整访问/]) {
      const item = screen.getByRole("menuitemradio", { name });
      expect(item.getAttribute("aria-disabled")).toBe("true");
      fireEvent.click(item);
      item.focus();
      fireEvent.keyDown(item, { key: "Enter" });
      fireEvent.keyDown(item, { key: " " });
    }
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText("当前执行环境尚未开放完整访问。")).toBeTruthy();
    expect(screen.getByText("服务器尚未启用自动审查。")).toBeTruthy();
    expect(screen.getByRole("menu").textContent).not.toMatch(/none|tool_approval_mode/);
    expect(screen.getByText(/仅规则审查.*非独立模型/)).toBeTruthy();
  });

  it("allows rule review only when available and keeps value controlled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PermissionSelector value="default" onChange={onChange} autoReviewAvailable />);
    await user.click(screen.getByRole("button", { name: /默认权限/ }));
    await user.click(screen.getByRole("menuitemradio", { name: /自动审查/ }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith("auto_review");
    expect(screen.getByRole("button", { name: /默认权限/ })).toBeTruthy();
  });

  it("navigates enabled options with wrapping arrows, Home and End", async () => {
    const user = userEvent.setup();
    render(<Controlled autoReviewAvailable={false} />);
    const trigger = screen.getByRole("button", { name: /默认权限/ });
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    const first = screen.getByRole("menuitemradio", { name: /默认权限/ });
    const last = screen.getByRole("menuitemradio", { name: /只读/ });
    expect(document.activeElement).toBe(first);
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(last);
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(first);
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(last);
    await user.keyboard("{Home}");
    expect(document.activeElement).toBe(first);
    await user.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(last);
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: /只读/ })).toBe(trigger);
  });

  it("closes on Escape and restores focus, but lets an outside click keep focus", async () => {
    const user = userEvent.setup();
    render(<><Controlled /><button>外部操作</button></>);
    const trigger = screen.getByRole("button", { name: /默认权限/ });
    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "外部操作" }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "外部操作" }));
  });

  it("lets Tab leave the portal menu through the trigger's normal tab order", async () => {
    const user = userEvent.setup();
    render(<><Controlled /><button>下一项</button></>);
    await user.click(screen.getByRole("button", { name: /默认权限/ }));
    await user.tab();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "下一项" }));
  });

  it("closes and prevents selection when disabled while open", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const view = render(<PermissionSelector value="default" onChange={onChange} autoReviewAvailable />);
    await user.click(screen.getByRole("button", { name: /默认权限/ }));
    view.rerender(<PermissionSelector value="default" onChange={onChange} autoReviewAvailable disabled />);
    expect(screen.queryByRole("menu")).toBeNull();
    await user.click(screen.getByRole("button", { name: /默认权限/ }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("rechecks rule availability while the menu is open", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const view = render(<PermissionSelector value="default" onChange={onChange} autoReviewAvailable />);
    await user.click(screen.getByRole("button", { name: /默认权限/ }));
    view.rerender(<PermissionSelector value="default" onChange={onChange} autoReviewAvailable={false} />);
    fireEvent.click(screen.getByRole("menuitemradio", { name: /自动审查/ }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("can disable all-tool read-only and show the negotiated legacy policy", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PermissionSelector value="default" onChange={onChange} autoReviewAvailable={false}
      readOnlyAvailable={false} permissionSupportNotice="旧服务沿用租户审查规则，不保证人工审批。" />);
    await user.click(screen.getByRole("button", { name: /沙箱权限.*默认权限/ }));
    expect(screen.getByText("旧服务沿用租户审查规则，不保证人工审批。")).toBeTruthy();
    const readOnly = screen.getByRole("menuitemradio", { name: /只读/ });
    expect(readOnly.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(readOnly);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("clamps the portal to a 320px viewport and opens above a bottom anchor", async () => {
    const user = userEvent.setup();
    vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(320);
    vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(480);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.getAttribute("role") === "menu"
        ? { width: 304, height: 340, top: 0, bottom: 340, left: 0, right: 304, x: 0, y: 0, toJSON() {} }
        : { width: 120, height: 32, top: 440, bottom: 472, left: 200, right: 320, x: 200, y: 440, toJSON() {} };
    });
    render(<Controlled />);
    await user.click(screen.getByRole("button", { name: /默认权限/ }));
    const menu = screen.getByRole("menu");
    await waitFor(() => {
      expect(Number.parseFloat(menu.style.left)).toBeGreaterThanOrEqual(8);
      expect(Number.parseFloat(menu.style.left) + Number.parseFloat(menu.style.width)).toBeLessThanOrEqual(312);
      expect(Number.parseFloat(menu.style.top)).toBeLessThan(440);
      expect(Number.parseFloat(menu.style.top)).toBeGreaterThanOrEqual(8);
      expect(Number.parseFloat(menu.style.maxHeight)).toBeGreaterThan(0);
      expect(Number.parseFloat(menu.style.top) + Math.min(340, Number.parseFloat(menu.style.maxHeight))).toBeLessThanOrEqual(472);
    });
    expect(within(menu).getAllByRole("menuitemradio")).toHaveLength(4);
  });

  it("keeps a positive usable height before layout reports menu dimensions", async () => {
    const user = userEvent.setup();
    vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(320);
    vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(480);
    render(<Controlled />);
    await user.click(screen.getByRole("button", { name: /沙箱权限.*默认权限/ }));
    const menu = screen.getByRole("menu", { name: "沙箱权限" });
    expect(Number.parseFloat(menu.style.maxHeight)).toBeGreaterThan(0);
    expect(Number.parseFloat(menu.style.top)).toBeGreaterThanOrEqual(8);
    expect(Number.parseFloat(menu.style.top) + Number.parseFloat(menu.style.maxHeight)).toBeLessThanOrEqual(480);
    await user.keyboard("{End}{Enter}");
    expect(screen.getByRole("button", { name: /沙箱权限.*只读/ })).toBeTruthy();
  });
});
