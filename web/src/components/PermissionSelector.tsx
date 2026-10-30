import { Check, ChevronDown, Eye, ShieldCheck, ShieldQuestion, ShieldOff } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import "./PermissionSelector.css";

export type PermissionMode = "default" | "auto_review" | "read_only" | "full_access";
export type SelectablePermissionMode = Exclude<PermissionMode, "full_access">;

export type PermissionSelectorProps = {
  value: PermissionMode;
  onChange: (mode: SelectablePermissionMode) => void;
  autoReviewAvailable: boolean;
  readOnlyAvailable?: boolean;
  permissionSupportNotice?: string;
  reviewSummary?: string;
  reviewDescription?: string;
  disabled?: boolean;
};

const OPTIONS = [
  { value: "default", label: "默认权限", Icon: ShieldQuestion, description: "在工作区边界内操作，需要审批的动作交由人工确认。" },
  { value: "auto_review", label: "自动审查", Icon: ShieldCheck, description: "仅规则审查，非独立模型；其余动作仍需人工确认。" },
  { value: "read_only", label: "只读", Icon: Eye, description: "仅读取，不授权修改或其他有副作用的工具操作。" },
  { value: "full_access", label: "完整访问权限", Icon: ShieldOff, description: "当前执行环境尚未开放完整访问。" },
] as const;

export function PermissionSelector({
  value, onChange, autoReviewAvailable, readOnlyAvailable = true, permissionSupportNotice, reviewSummary, reviewDescription, disabled = false,
}: PermissionSelectorProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 8, top: 8, width: 304, maxHeight: 400 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const initialFocus = useRef<"selected" | "first" | "last">("selected");
  const menuId = useId();
  const selected = OPTIONS.find((option) => option.value === value)!;
  const visible = open && !disabled;

  function unavailable(mode: PermissionMode) {
    return disabled || mode === "full_access" || (mode === "auto_review" && !autoReviewAvailable)
      || (mode === "read_only" && !readOnlyAvailable);
  }

  function close(restoreFocus: boolean) {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus({ preventScroll: true });
  }

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useLayoutEffect(() => {
    if (!visible) return;
    function place() {
      if (!triggerRef.current || !menuRef.current) return;
      const anchor = triggerRef.current.getBoundingClientRect();
      const viewport = window.visualViewport;
      const x = viewport?.offsetLeft ?? 0;
      const y = viewport?.offsetTop ?? 0;
      const width = viewport?.width ?? (document.documentElement.clientWidth || window.innerWidth);
      const height = viewport?.height ?? (document.documentElement.clientHeight || window.innerHeight);
      const menuWidth = Math.min(352, Math.max(0, width - 16));
      const above = Math.max(0, anchor.top - y - 16);
      const below = Math.max(0, y + height - anchor.bottom - 16);
      const measuredHeight = menuRef.current.getBoundingClientRect().height;
      const naturalHeight = Math.max(menuRef.current.scrollHeight, measuredHeight);
      const goesAbove = below < naturalHeight && above > below;
      const maxHeight = Math.min(480, goesAbove ? above : below);
      const menuHeight = Math.min(naturalHeight, maxHeight);
      setPosition({
        left: Math.min(Math.max(x + 8, anchor.left), x + width - menuWidth - 8),
        top: goesAbove ? Math.max(y + 8, anchor.top - menuHeight - 8) : Math.max(y + 8, anchor.bottom + 8),
        width: menuWidth,
        maxHeight,
      });
    }
    place();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    if (menuRef.current) observer?.observe(menuRef.current);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    window.visualViewport?.addEventListener("resize", place);
    window.visualViewport?.addEventListener("scroll", place);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      window.visualViewport?.removeEventListener("resize", place);
      window.visualViewport?.removeEventListener("scroll", place);
    };
  }, [visible, permissionSupportNotice, autoReviewAvailable, readOnlyAvailable, reviewDescription]);

  useEffect(() => {
    if (!visible) return;
    const items = menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]:not(:disabled)');
    const checked = menuRef.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]:not(:disabled)');
    const target = initialFocus.current === "last" ? items?.[items.length - 1]
      : initialFocus.current === "first" ? items?.[0] : checked ?? items?.[0];
    target?.focus({ preventScroll: true });
    function onPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !triggerRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }
    function onFocusIn(event: FocusEvent) {
      if (event.target instanceof Node && !triggerRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }
    function onEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      close(true);
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("focusin", onFocusIn);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("keydown", onEscape);
    };
  }, [visible]);

  return (
    <>
      <button ref={triggerRef} type="button" className="permission-selector-trigger" disabled={disabled}
        aria-label={`沙箱权限：${selected.label}${value === "auto_review" && reviewSummary ? ` · ${reviewSummary}` : ""}`} aria-haspopup="menu" aria-expanded={visible}
        aria-controls={visible ? menuId : undefined} title={`沙箱权限：${selected.label}${value === "auto_review" && reviewDescription ? `；${reviewDescription}` : ""}`}
        onClick={() => { initialFocus.current = "selected"; setOpen((current) => !current); }}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          event.preventDefault();
          initialFocus.current = event.key === "ArrowUp" ? "last" : "first";
          setOpen(true);
        }}>
        <selected.Icon size={16} aria-hidden="true" />
        <span>{value === "auto_review" && reviewSummary ? reviewSummary : selected.label}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {visible ? createPortal(
        <div ref={menuRef} id={menuId} role="menu" aria-label="沙箱权限" className="permission-selector-menu"
          style={position}
          onKeyDown={(event) => {
            if (event.key === "Tab") {
              // Resume the anchor's tab order instead of the body's portal position.
              event.preventDefault();
              const order = [...document.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, summary, [tabindex]')]
                .filter((element) => {
                  if (element.tabIndex < 0 || element.matches(":disabled") || menuRef.current?.contains(element)) return false;
                  for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
                    const style = getComputedStyle(ancestor);
                    if (ancestor.hidden || ancestor.inert || style.display === "none" || style.visibility === "hidden") return false;
                  }
                  return true;
                });
              const index = order.findIndex((element) => element === triggerRef.current);
              const next = order[index + (event.shiftKey ? -1 : 1)];
              if (next) next.focus();
              else (document.activeElement as HTMLElement | null)?.blur();
              setOpen(false);
              return;
            }
            if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]:not(:disabled)')];
            if (!items.length) return;
            const current = items.findIndex((item) => item === document.activeElement);
            const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
              : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
            items[index].focus();
          }}>
          {OPTIONS.map(({ value: mode, label, description, Icon }) => (
            <button key={mode} type="button" role="menuitemradio" tabIndex={-1}
              aria-checked={value === mode} aria-disabled={unavailable(mode)} disabled={unavailable(mode)}
              className="permission-selector-option"
              onClick={() => {
                if (mode === "full_access" || unavailable(mode)) return;
                onChange(mode);
                close(true);
              }}>
              <Icon size={18} aria-hidden="true" />
              <span className="permission-selector-copy">
                <strong>{label}</strong>
                <span>{mode === "default" && permissionSupportNotice ? permissionSupportNotice : mode === "auto_review" && reviewDescription ? reviewDescription : description}</span>
                {mode === "auto_review" && !autoReviewAvailable ? <small>服务器尚未启用自动审查。</small> : null}
                {mode === "read_only" && !readOnlyAvailable ? <small>当前服务仅支持工作区只读，尚不保证所有工具只读。</small> : null}
              </span>
              <span className="permission-selector-check">{value === mode ? <Check size={17} aria-hidden="true" /> : null}</span>
            </button>
          ))}
        </div>, document.body,
      ) : null}
    </>
  );
}
