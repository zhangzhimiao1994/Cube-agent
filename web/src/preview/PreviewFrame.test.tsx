import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WebPreviewSchema } from "../api/client";
import { PreviewFrame } from "./PreviewFrame";
import * as transport from "./transport";

function framePreview(kind: "static" | "dynamic") {
  const id = kind === "static" ? "10000000-0000-4000-8000-000000000010" : "10000000-0000-4000-8000-000000000011";
  return WebPreviewSchema.parse({
    id, status: "ready", preview_url: `/api/v1/web-previews/${id}/content/`,
    lease_expires_at: null, application_transport: kind === "dynamic",
    identity: {
      preview_id: id, kind, tenant_id: "33333333-3333-4333-8333-333333333333",
      user_id: "11111111-1111-4111-8111-111111111111", project_id: "project-frame",
      conversation_id: "conv-frame", workspace_session_id: "session-frame",
      runtime_handle: kind === "dynamic" ? "a".repeat(32) : null,
      source: { scheme: kind === "dynamic" ? "preview-broker-tree-v2" : "preview-static-tree-v1", sha256: "b".repeat(64) },
      display_root: ".", display_entrypoint: "index.html",
    },
    cleanup_url: `/api/v1/web-previews/${id}/cleanup`, cleanup_receipt: null,
  });
}

describe("opaque application frame lifecycle", () => {
  it("binds its own window, keeps sandbox opaque and revokes on unmount", () => {
    const dispose = vi.fn(); const frameLoaded = vi.fn();
    const attach = vi.spyOn(transport, "attachPreviewBridge").mockReturnValue({ dispose, frameLoaded });
    const onLoad = vi.fn();
    const { unmount } = render(<PreviewFrame preview={framePreview("dynamic")} title="Preview" onLoad={onLoad} />);
    const frame = screen.getByTitle("Preview");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-forms allow-modals");
    expect(attach.mock.calls[0][0]).toBe(frame);
    fireEvent.load(frame); expect(frameLoaded).toHaveBeenCalledOnce(); expect(onLoad).toHaveBeenCalledOnce();
    unmount(); expect(dispose).toHaveBeenCalledOnce(); attach.mockRestore();
  });
  it("does not attach an application transport to a static preview", () => {
    const attach = vi.spyOn(transport, "attachPreviewBridge");
    render(<PreviewFrame preview={framePreview("static")} title="Static" />);
    expect(attach).not.toHaveBeenCalled(); attach.mockRestore();
  });
});
