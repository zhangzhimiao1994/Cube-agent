import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api, WebPreviewSchema } from "../api/client";
import { WebsitePreviewPage } from "./WebsitePreviewPage";
afterEach(() => vi.restoreAllMocks());
const previewId = "10000000-0000-4000-8000-000000000008";
const preview = WebPreviewSchema.parse({
  id: previewId, status: "ready", preview_url: `/api/v1/web-previews/${previewId}/content/`,
  lease_expires_at: null, application_transport: false,
  identity: {
    preview_id: previewId, kind: "static", tenant_id: "33333333-3333-4333-8333-333333333333",
    user_id: "11111111-1111-4111-8111-111111111111", project_id: "project-owned",
    conversation_id: "conv-owned", workspace_session_id: "session-owned",
    runtime_handle: null, source: { scheme: "preview-static-tree-v1", sha256: "b".repeat(64) },
    display_root: ".", display_entrypoint: "index.html",
  },
  cleanup_url: `/api/v1/web-previews/${previewId}/cleanup`, cleanup_receipt: null,
});
function show() { render(<MemoryRouter initialEntries={[`/website-preview/${preview.id}?conversation=conv-owned`]}><Routes><Route path="/website-preview/:previewId" element={<WebsitePreviewPage />} /></Routes></MemoryRouter>); }
describe("authenticated standalone preview", () => {
  it("checks the canonical current preview before displaying it", async () => {
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(preview); show();
    expect(await screen.findByTitle("网站预览")).toBeTruthy();
    expect(current).toHaveBeenCalledWith("conv-owned");
  });
  it("does not show a replacement or another conversation's preview", async () => {
    const replacementId = "10000000-0000-4000-8000-000000000009";
    const replacement = WebPreviewSchema.parse({
      ...preview, id: replacementId, identity: { ...preview.identity, preview_id: replacementId },
      preview_url: `/api/v1/web-previews/${replacementId}/content/`,
      cleanup_url: `/api/v1/web-previews/${replacementId}/cleanup`,
    });
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(replacement); show();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByTitle("网站预览")).toBeNull();
  });
});
