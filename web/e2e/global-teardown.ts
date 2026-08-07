import { existsSync, readFileSync, rmSync } from "node:fs";
import { resolve } from "node:path";

const previewPidFile = resolve("test-results", "preview.pid");

export default async function globalTeardown() {
  if (!existsSync(previewPidFile)) return;
  const rawPid = readFileSync(previewPidFile, "utf8").trim();
  const pid = Number.parseInt(rawPid, 10);
  if (Number.isFinite(pid) && pid > 0) {
    try {
      process.kill(pid);
    } catch {
      // The preview process may already have exited.
    }
  }
  rmSync(previewPidFile, { force: true });
}
