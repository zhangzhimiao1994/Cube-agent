import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

export function useNavSection(paramNames: string[] = ["section"]) {
  const [searchParams] = useSearchParams();
  const activeSection = paramNames
    .map((name) => searchParams.get(name))
    .find((value): value is string => value !== null && value.trim() !== "") ?? null;

  useEffect(() => {
    if (!activeSection) return;
    const target = Array.from(document.querySelectorAll<HTMLElement>("[data-nav-section]")).find(
      (item) => item.dataset.navSection === activeSection,
    );
    if (!target) return;
    target.scrollIntoView?.({ block: "start", behavior: "smooth" });
    target.focus({ preventScroll: true });
  }, [activeSection]);

  function navTargetProps(section: string, className?: string) {
    const active = activeSection === section;
    return {
      "data-nav-section": section,
      "data-nav-active": active ? "true" : undefined,
      className: [className, active ? "nav-section-active" : null].filter(Boolean).join(" ") || undefined,
      tabIndex: -1,
    } as const;
  }

  return { activeSection, navTargetProps };
}