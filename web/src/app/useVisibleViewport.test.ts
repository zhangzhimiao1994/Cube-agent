import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { useVisibleViewport } from "./useVisibleViewport";

afterEach(() => vi.unstubAllGlobals());

it("tracks the actual visible area and removes listeners on unmount", () => {
  const viewport = Object.assign(new EventTarget(), { height: 560, width: 390, offsetTop: 45, offsetLeft: 0 });
  vi.stubGlobal("visualViewport", viewport);
  vi.stubGlobal("innerHeight", 844);
  vi.stubGlobal("innerWidth", 390);
  const { unmount } = renderHook(useVisibleViewport);
  const style = document.documentElement.style;
  expect(style.getPropertyValue("--visible-viewport-height")).toBe("560px");
  expect(style.getPropertyValue("--visible-viewport-top")).toBe("45px");
  act(() => { viewport.height = 420; viewport.offsetTop = 80; viewport.dispatchEvent(new Event("resize")); });
  expect(style.getPropertyValue("--visible-viewport-height")).toBe("420px");
  expect(style.getPropertyValue("--visible-viewport-top")).toBe("80px");
  act(() => { viewport.offsetTop = 95; viewport.dispatchEvent(new Event("scroll")); });
  expect(style.getPropertyValue("--visible-viewport-top")).toBe("95px");
  unmount();
  viewport.dispatchEvent(new Event("resize"));
  expect(style.getPropertyValue("--visible-viewport-height")).toBe("");
});

it("falls back to the window dimensions without the VisualViewport API", () => {
  vi.stubGlobal("visualViewport", undefined);
  vi.stubGlobal("innerHeight", 900);
  vi.stubGlobal("innerWidth", 1280);
  const { unmount } = renderHook(useVisibleViewport);
  expect(document.documentElement.style.getPropertyValue("--visible-viewport-height")).toBe("900px");
  act(() => { vi.stubGlobal("innerHeight", 650); window.dispatchEvent(new Event("resize")); });
  expect(document.documentElement.style.getPropertyValue("--visible-viewport-height")).toBe("650px");
  unmount();
});
