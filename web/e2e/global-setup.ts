import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const previewPidFile = resolve("test-results", "preview.pid");

async function waitForPreview(): Promise<void> {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch("http://127.0.0.1:4173");
      if (response.ok) return;
    } catch {
      // Retry until Vite preview binds the port.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
  }
  throw new Error("Vite preview did not become ready on 127.0.0.1:4173");
}

export default async function globalSetup() {
  const buildCommand = process.platform === "win32" ? "cmd.exe" : "npm";
  const buildArgs =
    process.platform === "win32" ? ["/c", "npm.cmd", "run", "build"] : ["run", "build"];
  const build = spawnSync(buildCommand, buildArgs, {
    cwd: process.cwd(),
    stdio: "inherit",
    shell: false,
  });
  if (build.status !== 0) {
    throw new Error(`frontend build failed before Playwright: ${build.error?.message ?? build.status}`);
  }

  mkdirSync(dirname(previewPidFile), { recursive: true });
  const child = spawn(
    process.execPath,
    ["./node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", "4173", "--strictPort"],
    {
      cwd: process.cwd(),
      detached: false,
      stdio: "ignore",
      windowsHide: true,
    },
  );
  if (child.pid === undefined) {
    throw new Error("failed to start Vite preview");
  }
  child.unref();
  writeFileSync(previewPidFile, String(child.pid), "utf8");
  await waitForPreview();
}
