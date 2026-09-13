import { useState } from "react";

import { api, formatApiError, type RunDetail } from "../api/client";

type ArtifactFile = (
  | RunDetail["artifacts"][number]
  | NonNullable<RunDetail["events"][number]["artifact"]>
) & {
  path?: string;
};
type FileLike = {
  id?: string;
  kind?: string | null;
  title?: string | null;
  text?: string | null;
  filename?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  download_url?: string | null;
  presentation?: string | null;
  path?: string | null;
};
export type DownloadableFile = FileLike & { download_url: string };

export function hasArtifactDownload(
  artifact: ArtifactFile | null | undefined,
): artifact is ArtifactFile & { download_url: string } {
  return typeof artifact?.download_url === "string" && artifact.download_url.trim().length > 0;
}

export function artifactFileName(artifact: FileLike) {
  return artifact.filename?.trim() || artifact.title || artifact.path || artifact.id || "download";
}

function isArchitectureGraphArtifact(artifact: FileLike) {
  const filename = artifactFileName(artifact).toLowerCase();
  const title = artifact.title?.toLowerCase() ?? "";
  const kind = artifact.kind?.toLowerCase() ?? "";
  const mimeType = artifact.mime_type?.toLowerCase() ?? "";
  return (
    filename === "architecture-map.html" ||
    kind === "project_architecture_graph" ||
    title.includes("架构图谱") ||
    (mimeType === "text/html" && filename.includes("architecture"))
  );
}

export function formatFileSize(sizeBytes: number | null | undefined) {
  if (typeof sizeBytes !== "number" || !Number.isFinite(sizeBytes) || sizeBytes < 0) return "";
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = sizeBytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

export function ArtifactFileCard({
  artifact,
  compact = false,
}: {
  artifact: DownloadableFile;
  compact?: boolean;
}) {
  const [downloading, setDownloading] = useState(false);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const filename = artifactFileName(artifact);
  const size = formatFileSize(artifact.size_bytes);
  const mimeType = artifact.mime_type?.trim();
  const checksum = artifact.sha256?.trim();
  const architectureGraph = isArchitectureGraphArtifact(artifact);
  const kindLabel = architectureGraph ? "架构图谱" : artifact.kind;
  const meta = [kindLabel, size, mimeType].filter(Boolean);

  async function fetchArtifactBlob() {
    return api.downloadGeneratedArtifact(artifact.download_url);
  }

  async function handleDownload() {
    setDownloading(true);
    setError(null);
    try {
      const blob = await fetchArtifactBlob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      anchor.rel = "noopener";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch (caught) {
      setError(formatApiError(caught, "文件下载失败"));
    } finally {
      setDownloading(false);
    }
  }

  async function handleOpen() {
    setOpening(true);
    setError(null);
    try {
      const blob = await fetchArtifactBlob();
      const objectUrl = URL.createObjectURL(blob);
      window.open(objectUrl, "_blank", "noopener,noreferrer");
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    } catch (caught) {
      setError(formatApiError(caught, "图谱打开失败"));
    } finally {
      setOpening(false);
    }
  }

  return (
    <div className={`artifact-file-card${compact ? " artifact-file-card-compact" : ""}`}>
      <span className="artifact-file-icon" aria-hidden="true">
        {architectureGraph ? "MAP" : "FILE"}
      </span>
      <div className="artifact-file-main">
        <strong>{filename}</strong>
        {meta.length > 0 ? (
          <small className="artifact-file-meta">
            {meta.map((item) => (
              <span key={item}>{item}</span>
            ))}
          </small>
        ) : null}
        {checksum ? <small title={checksum}>SHA-256 {checksum.slice(0, 12)}</small> : null}
      </div>
      <button
        type="button"
        disabled={downloading}
        onClick={() => void handleDownload()}
        aria-label={`下载 ${filename}`}
      >
        {downloading ? "下载中" : "下载"}
      </button>
      {architectureGraph ? (
        <button
          type="button"
          disabled={opening}
          onClick={() => void handleOpen()}
          aria-label={`打开 ${filename}`}
        >
          {opening ? "打开中" : "打开"}
        </button>
      ) : null}
      {error ? <small role="alert">{error}</small> : null}
    </div>
  );
}
