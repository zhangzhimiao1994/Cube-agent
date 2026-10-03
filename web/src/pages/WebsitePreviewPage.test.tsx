import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api } from "../api/client";
import { WebsitePreviewPage } from "./WebsitePreviewPage";
afterEach(() => vi.restoreAllMocks());
const preview = { id: "owned", status: "ready" as const, preview_url: "/preview", lease_expires_at: null, application_transport: false };
function show() { render(<MemoryRouter initialEntries={["/website-preview/owned?conversation=conv-owned"]}><Routes><Route path="/website-preview/:previewId" element={<WebsitePreviewPage />} /></Routes></MemoryRouter>); }
describe("authenticated standalone preview", () => {
  it("checks the canonical current preview before displaying it", async () => {
    const current = vi.spyOn(api, "webPreviewForConversation").mockResolvedValue(preview); show();
    expect(await screen.findByTitle("网站预览")).toBeTruthy();
    expect(current).toHaveBeenCalledWith("conv-owned");
  });
  it("does not show a replacement or another conversation's preview", async () => {
    vi.spyOn(api, "webPreviewForConversation").mockResolvedValue({ ...preview, id: "replacement" }); show();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByTitle("网站预览")).toBeNull();
  });
});
