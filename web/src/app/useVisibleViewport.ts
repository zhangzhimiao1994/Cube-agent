import { useLayoutEffect } from "react";

// Fixed overlays must fit the visible area, not the area behind mobile browser chrome or the keyboard.
export function useVisibleViewport() {
  useLayoutEffect(() => {
    const style = document.documentElement.style;
    const viewport = window.visualViewport;
    const names = ["height", "width", "top", "left"] as const;
    const previous = names.map((name) => style.getPropertyValue(`--visible-viewport-${name}`));
    const positive = (value: number | undefined, fallback: number) =>
      typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
    const offset = (value: number | undefined) =>
      typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0;
    const update = () => {
      const values = [Math.min(window.innerHeight, positive(viewport?.height, window.innerHeight)),
        Math.min(window.innerWidth, positive(viewport?.width, window.innerWidth)),
        offset(viewport?.offsetTop), offset(viewport?.offsetLeft)];
      names.forEach((name, index) => style.setProperty(`--visible-viewport-${name}`, `${values[index]}px`));
    };
    update();
    window.addEventListener("resize", update);
    viewport?.addEventListener("resize", update);
    viewport?.addEventListener("scroll", update);
    return () => {
      window.removeEventListener("resize", update);
      viewport?.removeEventListener("resize", update);
      viewport?.removeEventListener("scroll", update);
      names.forEach((name, index) => {
        const key = `--visible-viewport-${name}`;
        if (previous[index]) style.setProperty(key, previous[index]);
        else style.removeProperty(key);
      });
    };
  }, []);
}
