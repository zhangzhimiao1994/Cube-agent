import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { APP_BRAND_LOGO_SRC } from "./brand";

describe("shared light-surface brand", () => {
  it("uses a transparent PNG rather than the dark neon tile", () => {
    expect(APP_BRAND_LOGO_SRC).toBe("/brand/mofang-agent-light.png");
    const path = `public${APP_BRAND_LOGO_SRC}`;
    expect(existsSync(path)).toBe(true);
    const png = readFileSync(path);
    expect(png.subarray(1, 4).toString()).toBe("PNG");
    expect(png[25]).toBe(6);
  });
  it("keeps login, setup and navigation on the same asset", () => {
    for (const path of ["src/app/AppShell.tsx", "src/pages/LoginPage.tsx", "src/pages/SetupPage.tsx"]) {
      const source = readFileSync(path, "utf8");
      expect(source).toContain("src={APP_BRAND_LOGO_SRC}");
      expect(source).not.toContain("mofang-agent.jpg");
    }
  });
});
