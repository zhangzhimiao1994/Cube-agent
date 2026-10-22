import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { APP_BRAND_LOGO_SRC } from "./brand";

describe("shared light-surface brand", () => {
  it("retains the original transparent PNG as the hollow asset source", () => {
    const path = "public/brand/mofang-agent-light.png";
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

  it("hollows the original artwork with separate faint faces and readable contours", () => {
    expect(APP_BRAND_LOGO_SRC.endsWith(".svg")).toBe(true);
    const svg = new DOMParser().parseFromString(readFileSync(`public${APP_BRAND_LOGO_SRC}`, "utf8"), "image/svg+xml");
    expect(svg.querySelector("parsererror")).toBeNull();
    expect(svg.documentElement.getAttribute("viewBox")).toBe("0 0 1280 1280");
    const embedded = svg.querySelector("defs image")!.getAttribute("href")!;
    const original = Buffer.from(embedded.split(",")[1], "base64");
    expect(createHash("sha256").update(original).digest("hex")).toBe("1a9125acc8a2f72025f9763c0456a07d3eb999854257d14a73898f0727b901b0");
    const faces = svg.querySelectorAll("mask path[data-cube-face]");
    expect(faces).toHaveLength(12);
    for (const face of faces) {
      expect(face.getAttribute("fill")).toBe("black");
      expect(Number(face.getAttribute("fill-opacity"))).toBeGreaterThanOrEqual(0.85);
    }
    expect(Number(svg.querySelector('[result="orbit-wash"] feFuncA')!.getAttribute("slope"))).toBeLessThanOrEqual(0.15);
    expect(Number(svg.querySelector('[result="orbit-contours"] feFuncA')!.getAttribute("slope"))).toBeGreaterThanOrEqual(0.7);
    expect(svg.querySelector('feComposite[operator="out"]')).not.toBeNull();
    expect(Number(svg.querySelector('feMorphology[operator="erode"]')!.getAttribute("radius"))).toBeGreaterThanOrEqual(24);
    expect(Number(svg.querySelector('feColorMatrix[type="saturate"]')!.getAttribute("values"))).toBeLessThanOrEqual(0.25);
    expect(svg.querySelectorAll('svg > use')).toHaveLength(2);
    expect(svg.querySelector('svg > use[filter]')!.getAttribute("mask")).toBe("url(#orbits-only)");
  });
});
