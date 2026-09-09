import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArtifactFileCard, artifactFileName, hasArtifactDownload } from "./ArtifactFileCard";

const downloadable = {
  id: "artifact-zip",
  kind: "zip",
  title: "源码包",
  text: null,
  filename: "",
  mime_type: "application/zip",
  size_bytes: 18_432,
  sha256: "8f4d0c8d0e4d9d3a0a6a8e2e4b7a6c1d8e9f0a1b2c3d4e5f67890123456789ab",
  download_url: "/api/v1/admin/runs/run-1/artifacts/artifact-zip/download",
};
const userDownloadable = {
  ...downloadable,
  download_url: "/api/v1/runs/run-1/artifacts/artifact-zip/download",
};
const workspaceDownloadable = {
  ...downloadable,
  download_url: "/api/v1/workspaces/projects/project/sessions/session/files/download?path=src%2Fapp.py",
};
const workspaceBundleDownloadable = {
  ...downloadable,
  download_url: "/api/v1/workspaces/projects/project/sessions/session/bundle/download",
};

describe("ArtifactFileCard", () => {
  beforeEach(() => {
    window.sessionStorage.setItem("agent_hub_access_token", "owner-token");
  });

  afterEach(() => {
    cleanup();
    window.sessionStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders a safe download card with filename fallback and compact metadata", () => {
    render(<ArtifactFileCard artifact={downloadable} />);

    expect(artifactFileName(downloadable)).toBe("源码包");
    expect(hasArtifactDownload(downloadable)).toBe(true);
    expect(screen.getByText("源码包")).not.toBeNull();
    expect(screen.getByText("zip")).not.toBeNull();
    expect(screen.getByText("18.0 KB")).not.toBeNull();
    expect(screen.getByText("application/zip")).not.toBeNull();
    expect(screen.getByText(/SHA-256 8f4d0c8d0e4d/)).not.toBeNull();
    const download = screen.getByRole("button", { name: "下载 源码包" });
    expect(download).not.toBeNull();
  });

  it("downloads generated files with the current bearer token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["zip-bytes"], { type: "application/zip" }), {
        status: 200,
        headers: { "content-type": "application/zip" },
      }),
    );
    const objectUrl = "blob:artifact-download";
    const revokeObjectURL = vi.fn();
    const anchorClick = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => objectUrl),
      revokeObjectURL,
    });
    vi.spyOn(document, "createElement").mockImplementation((tagName) => {
      const element = document.createElementNS("http://www.w3.org/1999/xhtml", tagName);
      if (tagName.toLowerCase() === "a") {
        Object.defineProperty(element, "click", { value: anchorClick });
      }
      return element as HTMLElement;
    });

    render(<ArtifactFileCard artifact={downloadable} />);
    await userEvent.click(screen.getByRole("button", { name: "下载 源码包" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/v1\/admin\/runs\/run-1\/artifacts\/artifact-zip\/download\?_=/),
        expect.objectContaining({
          cache: "no-store",
          credentials: "include",
          headers: expect.objectContaining({ Authorization: "Bearer owner-token" }),
        }),
      );
      expect(anchorClick).toHaveBeenCalledTimes(1);
    });
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("downloads generated files from the user run download path", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["zip-bytes"], { type: "application/zip" }), {
        status: 200,
        headers: { "content-type": "application/zip" },
      }),
    );
    const objectUrl = "blob:artifact-download";
    const anchorClick = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => objectUrl),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(document, "createElement").mockImplementation((tagName) => {
      const element = document.createElementNS("http://www.w3.org/1999/xhtml", tagName);
      if (tagName.toLowerCase() === "a") {
        Object.defineProperty(element, "click", { value: anchorClick });
      }
      return element as HTMLElement;
    });

    render(<ArtifactFileCard artifact={userDownloadable} />);
    await userEvent.click(screen.getByRole("button", { name: "下载 源码包" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/v1\/runs\/run-1\/artifacts\/artifact-zip\/download\?_=/),
        expect.objectContaining({
          cache: "no-store",
          credentials: "include",
          headers: expect.objectContaining({ Authorization: "Bearer owner-token" }),
        }),
      );
      expect(anchorClick).toHaveBeenCalledTimes(1);
    });
  });

  it("downloads workspace files from backend-issued workspace paths", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["source"], { type: "text/plain" }), {
        status: 200,
        headers: { "content-type": "text/plain" },
      }),
    );
    const anchorClick = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:workspace-file"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(document, "createElement").mockImplementation((tagName) => {
      const element = document.createElementNS("http://www.w3.org/1999/xhtml", tagName);
      if (tagName.toLowerCase() === "a") {
        Object.defineProperty(element, "click", { value: anchorClick });
      }
      return element as HTMLElement;
    });

    render(<ArtifactFileCard artifact={workspaceDownloadable} />);
    await userEvent.click(screen.getByRole("button", { name: "下载 源码包" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(
          /^\/api\/v1\/workspaces\/projects\/project\/sessions\/session\/files\/download\?path=src%2Fapp\.py&_=/,
        ),
        expect.objectContaining({
          cache: "no-store",
          credentials: "include",
        }),
      );
      expect(anchorClick).toHaveBeenCalledTimes(1);
    });
  });

  it("downloads workspace bundles from backend-issued bundle paths", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["zip"], { type: "application/zip" }), {
        status: 200,
        headers: { "content-type": "application/zip" },
      }),
    );
    const anchorClick = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:workspace-bundle"),
      revokeObjectURL: vi.fn(),
    });
    vi.spyOn(document, "createElement").mockImplementation((tagName) => {
      const element = document.createElementNS("http://www.w3.org/1999/xhtml", tagName);
      if (tagName.toLowerCase() === "a") {
        Object.defineProperty(element, "click", { value: anchorClick });
      }
      return element as HTMLElement;
    });

    render(<ArtifactFileCard artifact={workspaceBundleDownloadable} />);
    await userEvent.click(screen.getByRole("button", { name: "下载 源码包" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(
          /^\/api\/v1\/workspaces\/projects\/project\/sessions\/session\/bundle\/download\?_=/,
        ),
        expect.objectContaining({
          cache: "no-store",
          credentials: "include",
        }),
      );
      expect(anchorClick).toHaveBeenCalledTimes(1);
    });
  });

  it("rejects workspace download paths with unsafe project segments before fetching", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["unsafe"], { type: "text/plain" }), {
        status: 200,
        headers: { "content-type": "text/plain" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ArtifactFileCard
        artifact={{
          ...workspaceBundleDownloadable,
          download_url: "/api/v1/workspaces/projects/../sessions/session/bundle/download",
        }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "下载 源码包" }));

    await waitFor(() => {
      expect(fetchMock).not.toHaveBeenCalled();
      expect(screen.getByRole("alert").textContent).toContain("unsupported download URL");
    });
  });

  it("requires only a non-empty download URL for download eligibility", () => {
    expect(hasArtifactDownload({ ...downloadable, filename: null })).toBe(true);
    expect(hasArtifactDownload({ ...downloadable, download_url: "   " })).toBe(false);
    expect(hasArtifactDownload(null)).toBe(false);
  });
});
