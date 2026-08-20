import { useCallback, useEffect } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

export function useNavSection(paramNames: string[] = ["section"]) {
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const activeSection = paramNames
    .map((name) => searchParams.get(name))
    .find((value): value is string => value !== null && value.trim() !== "") ?? null;

  const scrollToSection = useCallback((section: string) => {
    let cancelled = false;
    let timeoutId: number | undefined;
    let attempts = 0;

    const scrollToTarget = () => {
      if (cancelled) return;
      const target = Array.from(document.querySelectorAll<HTMLElement>("[data-nav-section]")).find(
        (item) => item.dataset.navSection === section,
      );
      if (target) {
        target.scrollIntoView?.({ block: "start", behavior: "smooth" });
        target.focus({ preventScroll: true });
        return;
      }
      attempts += 1;
      if (attempts < 8) {
        timeoutId = window.setTimeout(scrollToTarget, 50);
      }
    };

    timeoutId = window.setTimeout(scrollToTarget, 0);
    return () => {
      cancelled = true;
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  }, []);

  useEffect(() => {
    if (!activeSection) return;
    return scrollToSection(activeSection);
  }, [activeSection, location.pathname, location.search, scrollToSection]);

  useEffect(() => {
    const handleSectionNavigation = (event: Event) => {
      const section = (event as CustomEvent<{ section?: string }>).detail?.section;
      if (section) scrollToSection(section);
    };
    window.addEventListener("agent-hub:navigate-section", handleSectionNavigation);
    return () => window.removeEventListener("agent-hub:navigate-section", handleSectionNavigation);
  }, [scrollToSection]);

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