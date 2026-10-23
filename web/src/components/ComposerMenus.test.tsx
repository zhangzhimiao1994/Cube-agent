import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerModeMenu, ComposerMoreMenu, type ComposerMode } from "./ComposerMenus";
import { PermissionSelector } from "./PermissionSelector";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function moreProps(uploadsDisabled = false) {
  return { onImages: vi.fn(), onFiles: vi.fn(), onSettings: vi.fn(), onPlugins: vi.fn(), uploadsDisabled };
}

function ControlledMode() {
  const [value, onChange] = useState<ComposerMode>("auto");
  return <ComposerModeMenu value={value} onChange={onChange} disabled={false} />;
}

describe("ComposerMoreMenu", () => {
  it("opens a compact icon menu outside the composer overflow without locking page scroll", async () => {
    const user = userEvent.setup();
    const view = render(<div style={{ overflow: "hidden" }}><ComposerMoreMenu {...moreProps()} /></div>);
    const bodyOverflow = document.body.style.overflow;
    const rootOverflow = document.documentElement.style.overflow;
    const trigger = screen.getByRole("button", { name: "更多消息工具" });
    expect(trigger.textContent).toBe("");
    expect(trigger.querySelector("svg")).not.toBeNull();
    await user.click(trigger);
    const menu = screen.getByRole("menu", { name: "消息工具" });
    expect(view.container.contains(menu)).toBe(false);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(menu.id);
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent))
      .toEqual(["上传照片", "上传文件", "运行设置", "插件"]);
    for (const item of within(menu).getAllByRole("menuitem")) expect(item.querySelector("svg")).not.toBeNull();
    expect(menu.querySelector("small, p")).toBeNull();
    expect(document.body.style.overflow).toBe(bodyOverflow);
    expect(document.documentElement.style.overflow).toBe(rootOverflow);
  });

  it.each([
    ["上传照片", "onImages"], ["上传文件", "onFiles"],
    ["运行设置", "onSettings"], ["插件", "onPlugins"],
  ] as const)("invokes only the %s callback and closes", async (label, callback) => {
    const user = userEvent.setup();
    const props = moreProps();
    render(<ComposerMoreMenu {...props} />);
    await user.click(screen.getByRole("button", { name: "更多消息工具" }));
    await user.click(screen.getByRole("menuitem", { name: label }));
    expect(props[callback]).toHaveBeenCalledTimes(1);
    for (const key of ["onImages", "onFiles", "onSettings", "onPlugins"] as const) {
      if (key !== callback) expect(props[key]).not.toHaveBeenCalled();
    }
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it.each(["上传照片", "上传文件"])("restores the trigger before opening the %s picker", async (label) => {
    const user = userEvent.setup();
    const props = moreProps();
    const callback = label === "上传照片" ? props.onImages : props.onFiles;
    let focusAtPicker: Element | null = null;
    callback.mockImplementation(() => { focusAtPicker = document.activeElement; });
    render(<ComposerMoreMenu {...props} />);
    const trigger = screen.getByRole("button", { name: "更多消息工具" });
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: label }));
    expect(focusAtPicker).toBe(trigger);
    expect(document.activeElement).toBe(trigger);
  });

  it("disables only upload actions and skips them in keyboard navigation", async () => {
    const user = userEvent.setup();
    const props = moreProps(true);
    render(<ComposerMoreMenu {...props} />);
    await user.click(screen.getByRole("button", { name: "更多消息工具" }));
    for (const name of ["上传照片", "上传文件"]) {
      const item = screen.getByRole("menuitem", { name }) as HTMLButtonElement;
      expect(item.disabled).toBe(true);
      fireEvent.click(item);
    }
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "运行设置" }));
    await user.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "插件" }));
    expect(props.onImages).not.toHaveBeenCalled();
    expect(props.onFiles).not.toHaveBeenCalled();
  });

  it("accepts an external trigger ref and restores it on Escape", async () => {
    const user = userEvent.setup();
    const triggerRef = createRef<HTMLButtonElement>();
    render(<ComposerMoreMenu {...moreProps()} triggerRef={triggerRef} />);
    expect(triggerRef.current).toBe(screen.getByRole("button", { name: "更多消息工具" }));
    await user.click(triggerRef.current!);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(triggerRef.current);
  });

  it.each(["onImages", "onFiles", "onSettings", "onPlugins"] as const)("does not steal focus acquired by %s", async (callback) => {
    const user = userEvent.setup();
    const target = createRef<HTMLInputElement>();
    const props = moreProps();
    props[callback].mockImplementation(() => target.current!.focus());
    render(<><ComposerMoreMenu {...props} /><input ref={target} aria-label="动作焦点" /></>);
    await user.click(screen.getByRole("button", { name: "更多消息工具" }));
    const labels = { onImages: "上传照片", onFiles: "上传文件", onSettings: "运行设置", onPlugins: "插件" };
    await user.click(screen.getByRole("menuitem", { name: labels[callback] }));
    expect(document.activeElement).toBe(target.current);
  });

  it("lets a settings modal take focus and return it through the supplied ref", async () => {
    const user = userEvent.setup();
    function SettingsFlow() {
      const triggerRef = useRef<HTMLButtonElement>(null);
      const [open, setOpen] = useState(false);
      return <><ComposerMoreMenu {...moreProps()} triggerRef={triggerRef} onSettings={() => setOpen(true)} />
        {open ? <section role="dialog" aria-label="运行设置"><input autoFocus aria-label="设置输入" />
          <button onClick={() => { setOpen(false); triggerRef.current!.focus(); }}>关闭设置</button></section> : null}</>;
    }
    render(<SettingsFlow />);
    await user.click(screen.getByRole("button", { name: "更多消息工具" }));
    await user.click(screen.getByRole("menuitem", { name: "运行设置" }));
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "设置输入" }));
    await user.click(screen.getByRole("button", { name: "关闭设置" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "更多消息工具" }));
  });
});

describe("ComposerModeMenu", () => {
  it("shows only the mode label and exposes four choices plus restore auto", async () => {
    const user = userEvent.setup();
    render(<ControlledMode />);
    const trigger = screen.getByRole("button", { name: "模式：自动" });
    expect(trigger.textContent).toBe("模式");
    expect(trigger.title).toBe("模式：自动");
    await user.click(trigger);
    const menu = screen.getByRole("menu", { name: "运行模式" });
    expect(within(menu).getAllByRole("menuitemradio").map((item) => item.textContent))
      .toEqual(["直连", "派单", "讨论", "混合", "恢复自动"]);
    expect(within(menu).getByRole("separator")).toBeTruthy();
    expect(screen.getByRole("menuitemradio", { name: "恢复自动" }).getAttribute("aria-checked")).toBe("true");
    expect(menu.querySelector("small, p")).toBeNull();
    await user.click(screen.getByRole("menuitemradio", { name: "直连" }));
    expect(screen.getByRole("button", { name: "模式：直连" })).toBe(trigger);
    expect(document.activeElement).toBe(trigger);
    await user.click(trigger);
    expect(screen.getByRole("menuitemradio", { name: "直连" }).getAttribute("aria-checked")).toBe("true");
    await user.click(screen.getByRole("menuitemradio", { name: "恢复自动" }));
    expect(screen.getByRole("button", { name: "模式：自动" })).toBe(trigger);
  });

  it.each(["direct", "dispatch", "discuss", "hybrid", "auto"] as const)("keeps %s controlled by its parent", async (value) => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ComposerModeMenu value="auto" onChange={onChange} disabled={false} />);
    await user.click(screen.getByRole("button", { name: "模式：自动" }));
    const label = { direct: "直连", dispatch: "派单", discuss: "讨论", hybrid: "混合", auto: "恢复自动" }[value];
    await user.click(screen.getByRole("menuitemradio", { name: label }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith(value);
    expect(screen.getByRole("button", { name: "模式：自动" })).toBeTruthy();
  });

  it("supports wrapping arrows, Home and End without losing the selected item", async () => {
    const user = userEvent.setup();
    render(<ControlledMode />);
    const trigger = screen.getByRole("button", { name: "模式：自动" });
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("menuitemradio", { name: "直连" }));
    await user.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(screen.getByRole("menuitemradio", { name: "恢复自动" }));
    await user.keyboard("{Home}{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("menuitemradio", { name: "派单" }));
    await user.keyboard("{End}{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("menuitemradio", { name: "直连" }));
    await user.keyboard("{Enter}");
    await user.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(screen.getByRole("menuitemradio", { name: "恢复自动" }));
  });

  it("closes immediately when disabled and does not emit a mode change", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const view = render(<ComposerModeMenu value="auto" onChange={onChange} disabled={false} />);
    await user.click(screen.getByRole("button", { name: "模式：自动" }));
    view.rerender(<ComposerModeMenu value="auto" onChange={onChange} disabled />);
    expect(screen.queryByRole("menu")).toBeNull();
    const trigger = screen.getByRole("button", { name: "模式：自动" }) as HTMLButtonElement;
    expect(trigger.disabled).toBe(true);
    await user.click(trigger);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("shared composer menu boundaries", () => {
  it("closes via outside pointer or focus without stealing the new target", async () => {
    const user = userEvent.setup();
    render(<><ComposerMoreMenu {...moreProps()} /><button>外部操作</button></>);
    const trigger = screen.getByRole("button", { name: "更多消息工具" });
    const outside = screen.getByRole("button", { name: "外部操作" });
    await user.click(trigger);
    await user.click(outside);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(outside);
    await user.click(trigger);
    act(() => outside.focus());
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.activeElement).toBe(outside);
  });

  it.each([false, true])("uses the trigger tab order when shift=%s, excluding hidden and disabled controls", async (shift) => {
    const user = userEvent.setup();
    render(<><button>上一项</button><ControlledMode /><input type="hidden" /><button disabled>不可用</button>
      <div hidden><button>隐藏项</button></div><button>下一项</button></>);
    await user.click(screen.getByRole("button", { name: "模式：自动" }));
    await user.tab({ shift });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: shift ? "上一项" : "下一项" }));
  });

  it("opens only one of more, mode and independent permissions at a time", async () => {
    const user = userEvent.setup();
    render(<><ComposerMoreMenu {...moreProps()} /><ControlledMode />
      <PermissionSelector value="default" onChange={() => {}} autoReviewAvailable /></>);
    for (const [button, menu] of [["更多消息工具", "消息工具"], ["模式：自动", "运行模式"],
      ["沙箱权限：默认权限", "沙箱权限"], ["更多消息工具", "消息工具"]]) {
      await user.click(screen.getByRole("button", { name: button }));
      expect(screen.getAllByRole("menu")).toHaveLength(1);
      expect(screen.getByRole("menu", { name: menu })).toBeTruthy();
    }
  });

  it("keeps a positive menu height before browser layout reports a size", async () => {
    const user = userEvent.setup();
    render(<ControlledMode />);
    await user.click(screen.getByRole("button", { name: "模式：自动" }));
    const menu = screen.getByRole("menu", { name: "运行模式" });
    expect(Number.parseFloat(menu.style.maxHeight)).toBeGreaterThan(0);
    expect(Number.parseFloat(menu.style.top)).toBeGreaterThanOrEqual(8);
  });

  it("repositions inside an offset visualViewport after resize and scroll", async () => {
    const user = userEvent.setup();
    const viewport = Object.assign(new EventTarget(), { width: 320, height: 400, offsetLeft: 24, offsetTop: 50 });
    vi.stubGlobal("visualViewport", viewport);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return this.getAttribute("role") === "menu"
        ? { width: 224, height: 212, top: 0, bottom: 212, left: 0, right: 224, x: 0, y: 0, toJSON() {} }
        : { width: 70, height: 34, top: 400, bottom: 434, left: 300, right: 370, x: 300, y: 400, toJSON() {} };
    });
    render(<ControlledMode />);
    await user.click(screen.getByRole("button", { name: "模式：自动" }));
    const menu = screen.getByRole("menu", { name: "运行模式" });
    const inViewport = () => {
      const left = Number.parseFloat(menu.style.left), top = Number.parseFloat(menu.style.top);
      expect(left).toBeGreaterThanOrEqual(viewport.offsetLeft + 8);
      expect(left + Number.parseFloat(menu.style.width)).toBeLessThanOrEqual(viewport.offsetLeft + viewport.width - 8);
      expect(top).toBeGreaterThanOrEqual(viewport.offsetTop + 8);
      expect(top + Math.min(212, Number.parseFloat(menu.style.maxHeight))).toBeLessThanOrEqual(viewport.offsetTop + viewport.height - 8);
      expect(top).toBeLessThan(400);
    };
    await waitFor(inViewport);
    viewport.height = 280;
    viewport.offsetTop = 120;
    act(() => {
      viewport.dispatchEvent(new Event("resize"));
      viewport.dispatchEvent(new Event("scroll"));
    });
    await waitFor(inViewport);
  });
});
