import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PreviewFrame } from "./PreviewFrame";
import * as transport from "./transport";

describe("opaque application frame lifecycle", () => {
  it("binds its own window, keeps sandbox opaque and revokes on unmount", () => {
    const dispose = vi.fn(); const frameLoaded = vi.fn();
    const attach = vi.spyOn(transport, "attachPreviewBridge").mockReturnValue({ dispose, frameLoaded });
    const onLoad = vi.fn();
    const { unmount } = render(<PreviewFrame preview={{ id: "owned", status: "ready", preview_url: "/owned", lease_expires_at: null, application_transport: true }} title="Preview" onLoad={onLoad} />);
    const frame = screen.getByTitle("Preview");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-forms allow-modals");
    expect(attach.mock.calls[0][0]).toBe(frame);
    fireEvent.load(frame); expect(frameLoaded).toHaveBeenCalledOnce(); expect(onLoad).toHaveBeenCalledOnce();
    unmount(); expect(dispose).toHaveBeenCalledOnce(); attach.mockRestore();
  });
  it("does not attach an application transport to a static preview", () => {
    const attach = vi.spyOn(transport, "attachPreviewBridge");
    render(<PreviewFrame preview={{ id: "static", status: "ready", preview_url: "/static", lease_expires_at: null }} title="Static" />);
    expect(attach).not.toHaveBeenCalled(); attach.mockRestore();
  });
});
