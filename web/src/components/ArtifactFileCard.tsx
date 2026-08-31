import { useState } from "react";

import { api, formatApiError, type RunDetail } from "../api/client";

type ArtifactFile =
  | RunDetail["artifacts"][number]
  | NonNullable<RunDetail["events"][number]["artifact"]>;

export function hasArtifactDownload(
  artifact: ArtifactFile | null | undefined,
): artifact is ArtifactFile & { download_url: string } {
  return typeof artifact?.download_url === "string" && artifact.download_url.trim().length > 0;
}

export function artifactFileName(artifact: ArtifactFile) {
  return artifact.filename?.trim() || artifact.title || artifact.id;
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
  artifact: ArtifactFile & { download_url: string };
  compact?: boolean;
}) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const filename = artifactFileName(artifact);
  const size = formatFileSize(artifact.size_bytes);
  const mimeType = artifact.mime_type?.trim();
  const checksum = artifact.sha256?.trim();
  const meta = [artifact.kind, size, mimeType].filter(Boolean);

  async function handleDownload() {
    setDownloading(true);
    setError(null);
    try {
      const blob = await api.downloadGeneratedArtifact(artifact.download_url);
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

  return (
    <div className={`artifact-file-card${compact ? " artifact-file-card-compact" : ""}`}>
      <span className="artifact-file-icon" aria-hidden="true">
        FILE
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
      {error ? <small role="alert">{error}</small> : null}
    </div>
  );
}
