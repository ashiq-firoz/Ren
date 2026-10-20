"use client";

import { useId, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface SwitchProps {
  checked: boolean;
  onCheckedChange?: (checked: boolean) => void;
  disabled?: boolean;
  label?: ReactNode;
  description?: ReactNode;
  size?: "sm" | "md";
  id?: string;
  className?: string;
  "aria-label"?: string;
}

export function Switch({
  checked,
  onCheckedChange,
  disabled,
  label,
  description,
  size = "md",
  id,
  className,
  "aria-label": ariaLabel,
}: SwitchProps) {
  const autoId = useId();
  const switchId = id ?? autoId;
  const track = size === "sm" ? "h-[18px] w-8" : "h-5 w-9";
  const knob = size === "sm" ? "size-3.5" : "size-4";
  const shift = size === "sm" ? "translate-x-[14px]" : "translate-x-4";
  const button = (
    <button
      type="button"
      role="switch"
      id={switchId}
      aria-checked={checked}
      aria-label={ariaLabel ?? (typeof label === "string" ? label : undefined)}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onCheckedChange?.(!checked);
      }}
      className={cn(
        "relative inline-flex shrink-0 items-center rounded-full border transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed",
        track,
        checked ? "border-primary bg-primary" : "border-border-strong bg-surface-3",
      )}
    >
      <span
        className={cn(
          "pointer-events-none ml-[1px] inline-block rounded-full shadow-sm transition-transform duration-150 ease-out",
          knob,
          checked ? cn(shift, "bg-primary-fg") : "translate-x-0 bg-surface",
        )}
      />
    </button>
  );
  if (!label && !description) return <span className={className}>{button}</span>;
  return (
    <div className={cn("flex items-start justify-between gap-4", className)}>
      <label htmlFor={switchId} className="min-w-0 cursor-pointer">
        {label && <span className="block text-[13px] font-medium text-fg">{label}</span>}
        {description && <span className="mt-0.5 block text-xs text-muted">{description}</span>}
      </label>
      {button}
    </div>
  );
}

export interface CheckboxProps {
  checked: boolean;
  onCheckedChange?: (checked: boolean) => void;
  label?: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  className?: string;
  id?: string;
}

export function Checkbox({ checked, onCheckedChange, label, description, disabled, className, id }: CheckboxProps) {
  const autoId = useId();
  const cid = id ?? autoId;
  return (
    <div className={cn("flex items-start gap-2.5", className)}>
      <input
        id={cid}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onCheckedChange?.(e.target.checked)}
        className="mt-0.5 size-4 shrink-0 cursor-pointer rounded border-border-strong accent-[var(--text)] disabled:cursor-not-allowed"
      />
      {(label || description) && (
        <label htmlFor={cid} className="min-w-0 cursor-pointer select-none">
          {label && <span className="block text-[13px] text-fg">{label}</span>}
          {description && <span className="block text-xs text-muted">{description}</span>}
        </label>
      )}
    </div>
  );
}

export interface SliderProps {
  value: number;
  onValueChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
  id?: string;
}

export function Slider({ value, onValueChange, min = 0, max = 1, step = 0.01, disabled, className, id, ...rest }: SliderProps) {
  const pct = max === min ? 0 : ((value - min) / (max - min)) * 100;
  return (
    <input
      id={id}
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      disabled={disabled}
      aria-label={rest["aria-label"]}
      onChange={(e) => onValueChange(Number(e.target.value))}
      className={cn("range", className)}
      style={{ "--fill": `${pct}%` } as CSSProperties}
    />
  );
}

export interface SegmentedOption<T extends string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
  title?: string;
}

export interface SegmentedControlProps<T extends string> {
  value: T;
  onChange: (v: T) => void;
  options: SegmentedOption<T>[];
  size?: "sm" | "md";
  className?: string;
  "aria-label"?: string;
  fullWidth?: boolean;
}

export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  size = "md",
  className,
  fullWidth,
  ...rest
}: SegmentedControlProps<T>) {
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const idx = options.findIndex((o) => o.value === value);
    const next = options[(idx + (e.key === "ArrowRight" ? 1 : -1) + options.length) % options.length];
    onChange(next.value);
    const btn = e.currentTarget.querySelector<HTMLButtonElement>(`[data-value="${CSS.escape(next.value)}"]`);
    btn?.focus();
  };
  return (
    <div
      role="radiogroup"
      aria-label={rest["aria-label"]}
      onKeyDown={onKeyDown}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-control border border-border bg-surface-2 p-0.5",
        fullWidth && "flex w-full",
        className,
      )}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            data-value={o.value}
            title={o.title}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex items-center justify-center gap-1.5 rounded-[6px] font-medium transition-colors duration-150 [&_svg]:size-3.5",
              size === "sm" ? "h-6 px-2 text-xs" : "h-7 px-2.5 text-[13px]",
              fullWidth && "flex-1",
              active ? "bg-surface text-fg shadow-[0_1px_2px_rgb(0_0_0/0.06)] ring-1 ring-border" : "text-muted hover:text-fg",
            )}
          >
            {o.icon}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
