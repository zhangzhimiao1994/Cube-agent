import type { RunDetail } from "../api/client";

type ArtifactFile = RunDetail["artifacts"][number] | NonNullable<RunDetail["events"][number]["artifact"]>;

export function hasArtifactDownload(artifact: ArtifactFile | null | undefined): artifact is ArtifactFile & { download_url: string } {
  return typeof artifact?.download_url === "string" && artifact.download_url.trim().length > 0;
}

export function artifactFileName(artifact: ArtifactFile) {
  return artifact.filename?.trim() || artifact.title || artifact.id;
}

function formatFileSize(sizeBytes: number | null | undefined) {
  if (typeof sizeBytes !== "number" || !Number.isFinite(sizeBytes) || sizeBytes < 0) return "";
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = sizeBytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const precision = value >= 10 || Number.isInteger(value) ? 0 : 1;
  return `${value.toFixed(precision)} ${units[unitIndex]}`;
}

export function ArtifactFileCard({
  artifact,
  compact = false,
}: {
  artifact: ArtifactFile & { download_url: string };
  compact?: boolean;
}) {
  const filename = artifactFileName(artifact);
  const size = formatFileSize(artifact.size_bytes);
  const mimeType = artifact.mime_type?.trim();
  const checksum = artifact.sha256?.trim();
  const meta = [artifact.kind, size, mimeType].filter(Boolean);

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
      <a href={artifact.download_url} download={filename} aria-label={`下载 ${filename}`}>
        下载
      </a>
    </div>
  );
}
