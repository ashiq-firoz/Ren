"use client";

import { Check, ChevronRight } from "lucide-react";
import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import { cn } from "@/lib/utils";
import { Portal } from "./overlay";

type Side = "top" | "bottom" | "left" | "right";
type Align = "start" | "center" | "end";
export type Placement = Side | `${Side}-start` | `${Side}-end`;

const MARGIN = 8;

function computePosition(anchor: DOMRect, panelW: number, panelH: number, placement: Placement, offset: number) {
  const [initialSide, alignRaw] = placement.split("-") as [Side, Align | undefined];
  const align: Align = alignRaw ?? "center";
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let side = initialSide;
  if (side === "bottom" && anchor.bottom + offset + panelH > vh - MARGIN && anchor.top - offset - panelH >= MARGIN) side = "top";
  else if (side === "top" && anchor.top - offset - panelH < MARGIN && anchor.bottom + offset + panelH <= vh - MARGIN) side = "bottom";
  else if (side === "right" && anchor.right + offset + panelW > vw - MARGIN && anchor.left - offset - panelW >= MARGIN) side = "left";
  else if (side === "left" && anchor.left - offset - panelW < MARGIN && anchor.right + offset + panelW <= vw - MARGIN) side = "right";

  let top: number;
  let left: number;
  if (side === "top" || side === "bottom") {
    top = side === "bottom" ? anchor.bottom + offset : anchor.top - offset - panelH;
    left =
      align === "start" ? anchor.left : align === "end" ? anchor.right - panelW : anchor.left + anchor.width / 2 - panelW / 2;
  } else {
    left = side === "right" ? anchor.right + offset : anchor.left - offset - panelW;
    top =
      align === "start" ? anchor.top : align === "end" ? anchor.bottom - panelH : anchor.top + anchor.height / 2 - panelH / 2;
  }
  left = Math.min(Math.max(MARGIN, left), Math.max(MARGIN, vw - panelW - MARGIN));
  top = Math.min(Math.max(MARGIN, top), Math.max(MARGIN, vh - panelH - MARGIN));
  return { top, left, side };
}

export interface FloatingPanelProps {
  open: boolean;
  anchor: HTMLElement | null;
  placement?: Placement;
  offset?: number;
  /** Called on outside pointerdown / Escape. Omit for non-dismissable panels (e.g. tooltips). */
  onDismiss?: () => void;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  role?: string;
  id?: string;
  matchAnchorWidth?: boolean;
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
  "aria-label"?: string;
}

/** Fixed-position panel rendered in a portal and positioned next to `anchor` (flips to stay on screen). */
export function FloatingPanel({
  open,
  anchor,
  placement = "bottom-start",
  offset = 6,
  onDismiss,
  children,
  className,
  style,
  role,
  id,
  matchAnchorWidth,
  onMouseEnter,
  onMouseLeave,
  ...rest
}: FloatingPanelProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);

  const update = useCallback(() => {
    const panel = panelRef.current;
    if (!panel || !anchor) return;
    if (!anchor.isConnected) {
      panel.style.visibility = "hidden";
      return;
    }
    if (matchAnchorWidth) panel.style.minWidth = `${anchor.offsetWidth}px`;
    const rect = anchor.getBoundingClientRect();
    const { top, left, side } = computePosition(rect, panel.offsetWidth, panel.offsetHeight, placement, offset);
    panel.style.top = `${Math.round(top)}px`;
    panel.style.left = `${Math.round(left)}px`;
    panel.style.visibility = "visible";
    panel.dataset.side = side;
  }, [anchor, placement, offset, matchAnchorWidth]);

  useLayoutEffect(() => {
    if (open) update();
  }, [open, update, children]);

  useEffect(() => {
    if (!open) return;
    const onScroll = () => update();
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    const ro = new ResizeObserver(() => update());
    if (panelRef.current) ro.observe(panelRef.current);
    if (anchor) ro.observe(anchor);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
      ro.disconnect();
    };
  }, [open, update, anchor]);

  useEffect(() => {
    if (!open || !onDismiss) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element;
      if (panelRef.current?.contains(t) || anchor?.contains(t)) return;
      // Clicks inside another floating panel (e.g. a nested popover) don't dismiss this one.
      if (t.closest?.("[data-floating-panel]")) return;
      onDismiss();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onDismiss();
        anchor?.focus?.({ preventScroll: true });
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open, onDismiss, anchor]);

  if (!open) return null;
  return (
    <Portal>
      <div
        ref={panelRef}
        role={role}
        id={id}
        aria-label={rest["aria-label"]}
        data-floating-panel
        data-dismissable={onDismiss ? "" : undefined}
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        style={{ position: "fixed", top: 0, left: 0, visibility: "hidden", ...style }}
        className={cn("z-[70] animate-pop-in", className)}
      >
        {children}
      </div>
    </Portal>
  );
}

type TriggerElement = ReactElement<Record<string, unknown>>;

/* ------------------------------------------------------------------ */
/* Popover                                                             */
/* ------------------------------------------------------------------ */

export interface PopoverProps {
  trigger: TriggerElement;
  children: ReactNode | ((close: () => void) => ReactNode);
  placement?: Placement;
  className?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Open on hover as well as click (e.g. citation chips). */
  hover?: boolean;
  offset?: number;
  "aria-label"?: string;
}

export function Popover({
  trigger,
  children,
  placement = "bottom-start",
  className,
  open: controlledOpen,
  onOpenChange,
  hover,
  offset,
  ...rest
}: PopoverProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = controlledOpen ?? uncontrolledOpen;
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  // Hover intent: each pointer transition bumps `seq` so the delay timer restarts.
  const [intent, setIntent] = useState<{ kind: "idle" | "enter" | "leave"; seq: number }>({ kind: "idle", seq: 0 });
  const panelId = useId();

  const setOpen = useCallback(
    (v: boolean) => {
      if (controlledOpen === undefined) setUncontrolledOpen(v);
      onOpenChange?.(v);
    },
    [controlledOpen, onOpenChange],
  );
  const close = useCallback(() => setOpen(false), [setOpen]);

  useEffect(() => {
    if (!hover || intent.kind === "idle") return;
    const enter = intent.kind === "enter";
    const t = setTimeout(() => setOpen(enter), enter ? 220 : 180);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- restart only on new pointer intent
  }, [hover, intent]);

  if (!isValidElement(trigger)) return null;
  const props = trigger.props;
  const call = (fn: unknown, e: unknown) => {
    if (typeof fn === "function") fn(e);
  };
  const triggerEl = cloneElement(trigger, {
    ref: setAnchor,
    "aria-expanded": open,
    "aria-haspopup": "dialog",
    "aria-controls": open ? panelId : undefined,
    onClick: (e: React.MouseEvent) => {
      call(props.onClick, e);
      e.stopPropagation();
      setIntent((i) => ({ kind: "idle", seq: i.seq + 1 }));
      setOpen(!open);
    },
    onMouseEnter: (e: React.MouseEvent) => {
      call(props.onMouseEnter, e);
      if (hover) setIntent((i) => ({ kind: "enter", seq: i.seq + 1 }));
    },
    onMouseLeave: (e: React.MouseEvent) => {
      call(props.onMouseLeave, e);
      if (hover) setIntent((i) => ({ kind: "leave", seq: i.seq + 1 }));
    },
  });

  return (
    <>
      {triggerEl}
      <FloatingPanel
        open={open}
        anchor={anchor}
        placement={placement}
        offset={offset}
        onDismiss={close}
        role="dialog"
        id={panelId}
        aria-label={rest["aria-label"]}
        onMouseEnter={hover ? () => setIntent((i) => ({ kind: "enter", seq: i.seq + 1 })) : undefined}
        onMouseLeave={hover ? () => setIntent((i) => ({ kind: "leave", seq: i.seq + 1 })) : undefined}
        className={cn("rounded-card border border-border bg-surface text-fg shadow-popover", className)}
      >
        {typeof children === "function" ? children(close) : children}
      </FloatingPanel>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Tooltip                                                             */
/* ------------------------------------------------------------------ */

export function Tooltip({
  content,
  children,
  placement = "top",
  delay = 350,
  className,
}: {
  content: ReactNode;
  children: TriggerElement;
  placement?: Placement;
  delay?: number;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [intent, setIntent] = useState(0); // >0 while hovered/focused (bumped per entry)
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const id = useId();

  useEffect(() => {
    if (intent <= 0) return;
    const t = setTimeout(() => setOpen(true), delay);
    return () => clearTimeout(t);
  }, [intent, delay]);

  if (!isValidElement(children) || content === null || content === undefined || content === "") return children;
  const props = children.props;
  const call = (fn: unknown, e: unknown) => {
    if (typeof fn === "function") fn(e);
  };
  const show = () => setIntent((n) => Math.abs(n) + 1);
  const hide = () => {
    setIntent((n) => -Math.abs(n));
    setOpen(false);
  };
  const child = cloneElement(children, {
    ref: setAnchor,
    "aria-describedby": open ? id : undefined,
    onMouseEnter: (e: React.MouseEvent) => {
      call(props.onMouseEnter, e);
      show();
    },
    onMouseLeave: (e: React.MouseEvent) => {
      call(props.onMouseLeave, e);
      hide();
    },
    onFocus: (e: React.FocusEvent) => {
      call(props.onFocus, e);
      show();
    },
    onBlur: (e: React.FocusEvent) => {
      call(props.onBlur, e);
      hide();
    },
    onPointerDown: (e: React.PointerEvent) => {
      call(props.onPointerDown, e);
      hide();
    },
  });
  return (
    <>
      {child}
      <FloatingPanel
        open={open}
        anchor={anchor}
        placement={placement}
        offset={6}
        role="tooltip"
        id={id}
        className={cn(
          "pointer-events-none max-w-xs rounded-md bg-primary px-2 py-1 text-[11.5px] leading-snug font-medium text-primary-fg shadow-popover",
          className,
        )}
      >
        {content}
      </FloatingPanel>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* DropdownMenu                                                        */
/* ------------------------------------------------------------------ */

export type MenuItem =
  | {
      type?: "item";
      label: ReactNode;
      icon?: ReactNode;
      onSelect?: () => void;
      href?: string;
      danger?: boolean;
      disabled?: boolean;
      checked?: boolean;
      hint?: ReactNode;
      description?: ReactNode;
      keepOpen?: boolean;
      submenu?: boolean;
    }
  | { type: "separator" }
  | { type: "label"; label: ReactNode }
  | { type: "custom"; render: (close: () => void) => ReactNode };

export interface DropdownMenuProps {
  trigger: TriggerElement;
  items: MenuItem[];
  placement?: Placement;
  className?: string;
  header?: ReactNode;
  footer?: ReactNode;
  "aria-label"?: string;
  onOpenChange?: (open: boolean) => void;
}

export function DropdownMenu({ trigger, items, placement = "bottom-end", className, header, footer, onOpenChange, ...rest }: DropdownMenuProps) {
  const [open, setOpenState] = useState(false);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  const setOpen = useCallback(
    (v: boolean) => {
      setOpenState(v);
      onOpenChange?.(v);
    },
    [onOpenChange],
  );
  const close = useCallback(() => setOpen(false), [setOpen]);

  useEffect(() => {
    if (!open) return;
    const raf = requestAnimationFrame(() => {
      listRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])')?.focus();
    });
    return () => cancelAnimationFrame(raf);
  }, [open]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const nodes = Array.from(
      listRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? [],
    );
    if (nodes.length === 0) return;
    const idx = nodes.indexOf(document.activeElement as HTMLElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      nodes[(idx + 1) % nodes.length].focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      nodes[(idx - 1 + nodes.length) % nodes.length].focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      nodes[0].focus();
    } else if (e.key === "End") {
      e.preventDefault();
      nodes[nodes.length - 1].focus();
    } else if (e.key === "Tab") {
      close();
    }
  };

  if (!isValidElement(trigger)) return null;
  const props = trigger.props;
  const triggerEl = cloneElement(trigger, {
    ref: setAnchor,
    "aria-haspopup": "menu",
    "aria-expanded": open,
    "aria-controls": open ? menuId : undefined,
    onClick: (e: React.MouseEvent) => {
      if (typeof props.onClick === "function") props.onClick(e);
      e.stopPropagation();
      setOpen(!open);
    },
    onKeyDown: (e: React.KeyboardEvent) => {
      if (typeof props.onKeyDown === "function") props.onKeyDown(e);
      if (e.key === "ArrowDown" && !open) {
        e.preventDefault();
        setOpen(true);
      }
    },
  });

  return (
    <>
      {triggerEl}
      <FloatingPanel
        open={open}
        anchor={anchor}
        placement={placement}
        onDismiss={close}
        className={cn(
          "min-w-[200px] overflow-hidden rounded-card border border-border bg-surface text-fg shadow-popover",
          className,
        )}
      >
        {header}
        <div
          ref={listRef}
          role="menu"
          id={menuId}
          aria-label={rest["aria-label"]}
          onKeyDown={onKeyDown}
          className="max-h-[min(70vh,480px)] overflow-y-auto p-1"
        >
          {items.map((item, i) => {
            if (item.type === "separator") return <div key={i} role="separator" className="my-1 h-px bg-border" />;
            if (item.type === "label")
              return (
                <div key={i} className="px-2.5 pt-2 pb-1 text-[11px] font-medium tracking-wide text-subtle uppercase">
                  {item.label}
                </div>
              );
            if (item.type === "custom") return <div key={i}>{item.render(close)}</div>;
            const content = (
              <>
                {item.icon && <span className="flex text-muted [&_svg]:size-4">{item.icon}</span>}
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{item.label}</span>
                  {item.description && <span className="block truncate text-xs text-muted">{item.description}</span>}
                </span>
                {item.hint && <span className="text-xs text-subtle">{item.hint}</span>}
                {item.checked && <Check className="size-4 text-fg" />}
                {item.submenu && <ChevronRight className="size-4 text-subtle" />}
              </>
            );
            const cls = cn(
              "flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-[13px] outline-none transition-colors",
              item.disabled
                ? "cursor-not-allowed opacity-45"
                : item.danger
                  ? "text-danger hover:bg-[color-mix(in_srgb,var(--danger)_9%,transparent)] focus:bg-[color-mix(in_srgb,var(--danger)_9%,transparent)]"
                  : "hover:bg-surface-2 focus:bg-surface-2",
            );
            const select = () => {
              if (item.disabled) return;
              if (!item.keepOpen) close();
              item.onSelect?.();
            };
            if (item.href && !item.disabled) {
              return (
                <a key={i} role="menuitem" tabIndex={-1} href={item.href} className={cls} onClick={() => close()}>
                  {content}
                </a>
              );
            }
            return (
              <div
                key={i}
                role="menuitem"
                tabIndex={-1}
                aria-disabled={item.disabled || undefined}
                className={cls}
                onClick={select}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    select();
                  }
                }}
              >
                {content}
              </div>
            );
          })}
        </div>
        {footer}
      </FloatingPanel>
    </>
  );
}
