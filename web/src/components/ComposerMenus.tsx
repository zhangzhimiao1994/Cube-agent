import { Blocks, Check, ChevronDown, GitMerge, ImagePlus, MessagesSquare, Paperclip, Plus, RotateCcw, Settings2, Workflow, Zap } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { createPortal } from "react-dom";

import "./ComposerMenus.css";

export type ComposerMode = "auto" | "direct" | "dispatch" | "discuss" | "hybrid";

type ComposerMoreMenuProps = {
  onImages: () => void;
  onFiles: () => void;
  onSettings: () => void;
  onPlugins: () => void;
  uploadsDisabled: boolean;
  triggerRef?: RefObject<HTMLButtonElement | null>;
};

type ComposerModeMenuProps = {
  value: ComposerMode;
  onChange: (mode: ComposerMode) => void;
  disabled: boolean;
};

const MODE_OPTIONS = [
  { value: "direct", label: "直连", Icon: Zap },
  { value: "dispatch", label: "派单", Icon: Workflow },
  { value: "discuss", label: "讨论", Icon: MessagesSquare },
  { value: "hybrid", label: "混合", Icon: GitMerge },
  { value: "auto", label: "恢复自动", Icon: RotateCcw },
] as const;

function useComposerMenu(disabled = false, suppliedRef?: RefObject<HTMLButtonElement | null>) {
  const internalRef = useRef<HTMLButtonElement>(null);
  const triggerRef = suppliedRef ?? internalRef;
  const menuRef = useRef<HTMLDivElement>(null);
  const initialFocus = useRef<"selected" | "first" | "last">("selected");
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 8, top: 8, width: 224, maxHeight: 400 });
  const visible = open && !disabled;

  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus({ preventScroll: true });
  }

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useLayoutEffect(() => {
    if (!visible) return;
    const viewport = window.visualViewport;
    function place() {
      if (!triggerRef.current || !menuRef.current) return;
      const anchor = triggerRef.current.getBoundingClientRect();
      const x = viewport?.offsetLeft ?? 0;
      const y = viewport?.offsetTop ?? 0;
      const width = viewport?.width ?? (document.documentElement.clientWidth || window.innerWidth);
      const height = viewport?.height ?? (document.documentElement.clientHeight || window.innerHeight);
      const menuWidth = Math.min(224, Math.max(0, width - 16));
      const available = Math.max(0, height - 16);
      const above = Math.min(available, Math.max(0, anchor.top - y - 16));
      const below = Math.min(available, Math.max(0, y + height - anchor.bottom - 16));
      const measuredHeight = menuRef.current.getBoundingClientRect().height;
      const naturalHeight = Math.max(menuRef.current.scrollHeight, measuredHeight)
        || menuRef.current.querySelectorAll("button").length * 40 + 22;
      const goesAbove = below < naturalHeight && above > below;
      const maxHeight = Math.min(480, goesAbove ? above : below);
      const menuHeight = Math.min(naturalHeight, maxHeight);
      setPosition({
        left: Math.max(x + 8, Math.min(anchor.left, x + width - menuWidth - 8)),
        top: Math.max(y + 8, Math.min(goesAbove ? anchor.top - menuHeight - 8 : anchor.bottom + 8, y + height - menuHeight - 8)),
        width: menuWidth,
        maxHeight,
      });
    }
    place();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    if (menuRef.current) observer?.observe(menuRef.current);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    viewport?.addEventListener("resize", place);
    viewport?.addEventListener("scroll", place);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      viewport?.removeEventListener("resize", place);
      viewport?.removeEventListener("scroll", place);
    };
  }, [visible, triggerRef]);

  useEffect(() => {
    if (!visible) return;
    const items = menuRef.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)');
    const checked = menuRef.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]:not(:disabled)');
    const target = initialFocus.current === "last" ? items?.[items.length - 1]
      : initialFocus.current === "first" ? items?.[0] : checked ?? items?.[0];
    target?.focus({ preventScroll: true });
    function outside(event: Event) {
      if (event.target instanceof Node && !triggerRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }
    function escape(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus({ preventScroll: true });
    }
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("focusin", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [visible, triggerRef]);

  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Tab") {
      // Resume the trigger's tab order, not the body's portal position.
      const order = [...document.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, summary, [tabindex]')]
        .filter((element) => {
          if (element.tabIndex < 0 || element.matches(':disabled, input[type="hidden"]') || menuRef.current?.contains(element)) return false;
          for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
            const style = getComputedStyle(ancestor);
            if (ancestor.hidden || ancestor.inert || style.display === "none" || style.visibility === "hidden") return false;
          }
          return true;
        });
      const index = order.findIndex((element) => element === triggerRef.current);
      const next = order[index + (event.shiftKey ? -1 : 1)];
      if (next) {
        event.preventDefault();
        next.focus();
      } else {
        triggerRef.current?.focus({ preventScroll: true });
      }
      close();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
    if (!items.length) return;
    const current = items.findIndex((item) => item === document.activeElement);
    const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
      : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items[index].focus({ preventScroll: true });
  }

  const triggerProps = {
    ref: triggerRef,
    "aria-haspopup": "menu" as const,
    "aria-expanded": visible,
    "aria-controls": visible ? menuId : undefined,
    onClick: () => { initialFocus.current = "selected"; setOpen((current) => !current); },
    onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      initialFocus.current = event.key === "ArrowUp" ? "last" : "first";
      setOpen(true);
    },
  };

  const menuProps = { ref: menuRef, id: menuId, role: "menu", className: "composer-menu", style: position, onKeyDown: onMenuKeyDown };
  return { visible, close, triggerProps, menuProps };
}

export function ComposerMoreMenu({ onImages, onFiles, onSettings, onPlugins, uploadsDisabled, triggerRef }: ComposerMoreMenuProps) {
  const menu = useComposerMenu(false, triggerRef);
  const options = [
    { label: "上传照片", Icon: ImagePlus, action: onImages, disabled: uploadsDisabled, restoreFocus: true },
    { label: "上传文件", Icon: Paperclip, action: onFiles, disabled: uploadsDisabled, restoreFocus: true },
    { label: "运行设置", Icon: Settings2, action: onSettings, disabled: false, restoreFocus: false },
    { label: "插件", Icon: Blocks, action: onPlugins, disabled: false, restoreFocus: false },
  ];
  return <>
    <button {...menu.triggerProps} type="button" className="composer-menu-trigger composer-more-trigger"
      aria-label="更多消息工具" title="更多消息工具"><Plus size={19} aria-hidden="true" /></button>
    {menu.visible ? createPortal(
      <div {...menu.menuProps} aria-label="消息工具">
        {options.map(({ label, Icon, action, disabled, restoreFocus }) => (
          <button key={label} type="button" role="menuitem" tabIndex={-1} disabled={disabled}
            className="composer-menu-option" onClick={() => {
              if (disabled) return;
              menu.close(restoreFocus);
              action();
            }}><Icon size={18} aria-hidden="true" /><span>{label}</span></button>
        ))}
      </div>, document.body,
    ) : null}
  </>;
}

export function ComposerModeMenu({ value, onChange, disabled }: ComposerModeMenuProps) {
  const menu = useComposerMenu(disabled);
  const label = value === "auto" ? "自动" : MODE_OPTIONS.find((option) => option.value === value)!.label;
  return <>
    <button {...menu.triggerProps} type="button" className="composer-menu-trigger composer-mode-trigger" disabled={disabled}
      aria-label={`模式：${label}`} title={`模式：${label}`}><span>模式</span><ChevronDown size={14} aria-hidden="true" /></button>
    {menu.visible ? createPortal(
      <div {...menu.menuProps} aria-label="运行模式">
        {MODE_OPTIONS.map(({ value: mode, label: optionLabel, Icon }) => (
          <div key={mode}>
            {mode === "auto" ? <div role="separator" className="composer-menu-separator" /> : null}
            <button type="button" role="menuitemradio" tabIndex={-1} aria-checked={value === mode}
              className="composer-menu-option" onClick={() => {
                if (disabled) return;
                menu.close(true);
                onChange(mode);
              }}><Icon size={18} aria-hidden="true" /><span>{optionLabel}</span>
              <span className="composer-menu-check">{value === mode ? <Check size={16} aria-hidden="true" /> : null}</span>
            </button>
          </div>
        ))}
      </div>, document.body,
    ) : null}
  </>;
}
