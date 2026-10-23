import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { APP_BRAND_LOGO_SRC } from "./brand";

describe("shared light-surface brand", () => {
  it("retains the original transparent PNG as the soft asset source", () => {
    const path = "public/brand/mofang-agent-light.png";
    expect(existsSync(path)).toBe(true);
    const png = readFileSync(path);
    expect(png.subarray(1, 4).toString()).toBe("PNG");
    expect(png[25]).toBe(6);
  });
  it("keeps login, setup and navigation on the same asset", () => {
    expect(APP_BRAND_LOGO_SRC).toBe("/brand/mofang-agent-soft.svg");
    for (const path of ["src/app/AppShell.tsx", "src/pages/LoginPage.tsx", "src/pages/SetupPage.tsx"]) {
      const source = readFileSync(path, "utf8");
      expect(source).toContain("src={APP_BRAND_LOGO_SRC}");
      expect(source).not.toContain("mofang-agent.jpg");
    }
  });

  it("preserves original embedded pixels, twelve faces and all contour coordinates", () => {
    expect(APP_BRAND_LOGO_SRC.endsWith(".svg")).toBe(true);
    const svg = new DOMParser().parseFromString(readFileSync(`public${APP_BRAND_LOGO_SRC}`, "utf8"), "image/svg+xml");
    expect(svg.querySelector("parsererror")).toBeNull();
    expect(svg.documentElement.getAttribute("viewBox")).toBe("0 0 1280 1280");
    const embedded = svg.querySelector("defs image")!.getAttribute("href")!;
    const original = Buffer.from(embedded.split(",")[1], "base64");
    expect(createHash("sha256").update(original).digest("hex")).toBe("1a9125acc8a2f72025f9763c0456a07d3eb999854257d14a73898f0727b901b0");
    expect(original.equals(readFileSync("public/brand/mofang-agent-light.png"))).toBe(true);
    const faces = svg.querySelectorAll("mask path[data-cube-face]");
    expect(faces).toHaveLength(12);
    const geometry = Array.from(svg.querySelectorAll("mask path"), (path) => ({
      mask: path.parentElement!.id,
      face: path.getAttribute("data-cube-face"),
      d: path.getAttribute("d"),
    }));
    expect(geometry).toHaveLength(14);
    expect(createHash("sha256").update(JSON.stringify(geometry)).digest("hex"))
      .toBe("1f7f9ce0fc247b01eb8a505895e527d77c7ff4f9254639b79a2e77565e1f15c1");
    expect(svg.querySelector('feComposite[operator="out"]')).not.toBeNull();
    expect(Number(svg.querySelector('feMorphology[operator="erode"]')!.getAttribute("radius"))).toBe(24);
    expect(svg.querySelectorAll('svg > use')).toHaveLength(2);
    expect(svg.querySelector('svg > use[filter]')!.getAttribute("mask")).toBe("url(#orbits-only)");
  });

  it("keeps gentle color rather than compressing the original artwork into gray lines", () => {
    const svg = new DOMParser().parseFromString(readFileSync(`public${APP_BRAND_LOGO_SRC}`, "utf8"), "image/svg+xml");
    expect(Number(svg.querySelector('#muted feColorMatrix[type="saturate"]')!.getAttribute("values"))).toBe(0.5);
    for (const channel of ["R", "G", "B"]) {
      const transfer = svg.querySelector(`#muted feFunc${channel}`)!;
      expect(transfer.getAttribute("type")).toBe("linear");
      expect(Number(transfer.getAttribute("slope"))).toBe(0.7);
      expect(Number(transfer.getAttribute("intercept"))).toBe(0.18);
    }
  });

  it("retains translucent colored face panels instead of near-empty cutouts", () => {
    const svg = new DOMParser().parseFromString(readFileSync(`public${APP_BRAND_LOGO_SRC}`, "utf8"), "image/svg+xml");
    const frameAlpha = Number(svg.querySelector('#cube-frame > path[fill="white"]')!.getAttribute("fill-opacity"));
    expect(frameAlpha).toBe(0.85);
    const faces = svg.querySelectorAll("#cube-frame path[data-cube-face]");
    expect(faces).toHaveLength(12);
    for (const face of faces) {
      expect(face.getAttribute("fill")).toBe("black");
      const cutout = Number(face.getAttribute("fill-opacity"));
      expect(cutout).toBe(0.28);
      expect(frameAlpha * (1 - cutout)).toBeCloseTo(0.612, 3);
    }
  });

  it("balances light orbit color with a visible but unobtrusive contour", () => {
    const svg = new DOMParser().parseFromString(readFileSync(`public${APP_BRAND_LOGO_SRC}`, "utf8"), "image/svg+xml");
    const wash = Number(svg.querySelector('[result="orbit-wash"] feFuncA')!.getAttribute("slope"));
    const contour = Number(svg.querySelector('[result="orbit-contours"] feFuncA')!.getAttribute("slope"));
    expect(wash).toBe(0.5);
    expect(contour).toBe(0.35);
    expect(contour + wash * (1 - contour)).toBeCloseTo(0.675, 3);
    expect(Array.from(svg.querySelectorAll('[result="hollow-alpha"] feMergeNode'),
      (node) => node.getAttribute("in"))).toEqual(["orbit-wash", "orbit-contours"]);
  });
});
