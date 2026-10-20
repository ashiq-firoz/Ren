"use client";

import { ChevronDown } from "lucide-react";
import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  type ComponentProps,
  type ReactElement,
  type ReactNode,
} from "react";
import { cn } from "@/lib/utils";

export const controlClass =
  "w-full rounded-control border border-border-strong bg-surface text-fg text-sm transition-[border-color,box-shadow] duration-150 placeholder:text-subtle focus:outline-none focus-visible:outline-none focus:border-focus focus:ring-[3px] focus:ring-[color-mix(in_srgb,var(--focus)_18%,transparent)] disabled:opacity-60 disabled:cursor-not-allowed aria-[invalid=true]:border-danger";

export interface InputProps extends Omit<ComponentProps<"input">, "size"> {
  leftIcon?: ReactNode;
  rightSlot?: ReactNode;
  invalid?: boolean;
  inputSize?: "sm" | "md";
  wrapperClassName?: string;
}

export function Input({
  leftIcon,
  rightSlot,
  invalid,
  inputSize = "md",
  className,
  wrapperClassName,
  ...props
}: InputProps) {
  const h = inputSize === "sm" ? "h-8 text-[13px]" : "h-9";
  if (!leftIcon && !rightSlot) {
    return (
      <input
        aria-invalid={invalid || undefined}
        className={cn(controlClass, h, "px-3", className)}
        {...props}
      />
    );
  }
  return (
    <div className={cn("relative flex items-center", wrapperClassName)}>
      {leftIcon && (
        <span className="pointer-events-none absolute left-2.5 flex text-subtle [&_svg]:size-4">{leftIcon}</span>
      )}
      <input
        aria-invalid={invalid || undefined}
        className={cn(controlClass, h, leftIcon ? "pl-8" : "pl-3", rightSlot ? "pr-9" : "pr-3", className)}
        {...props}
      />
      {rightSlot && <span className="absolute right-1.5 flex items-center">{rightSlot}</span>}
    </div>
  );
}

export interface TextareaProps extends ComponentProps<"textarea"> {
  autosize?: boolean;
  minRows?: number;
  maxRows?: number;
  invalid?: boolean;
  mono?: boolean;
}

export function Textarea({
  autosize = false,
  minRows = 3,
  maxRows = 16,
  invalid,
  mono,
  className,
  ref,
  value,
  onChange,
  ...props
}: TextareaProps) {
  const innerRef = useRef<HTMLTextAreaElement | null>(null);

  const resize = useCallback(() => {
    const el = innerRef.current;
    if (!el || !autosize) return;
    const style = window.getComputedStyle(el);
    const line = parseFloat(style.lineHeight) || 20;
    const pad = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    el.style.height = "auto";
    const min = line * minRows + pad + border;
    const max = line * maxRows + pad + border;
    const next = Math.min(max, Math.max(min, el.scrollHeight + border));
    el.style.height = `${next}px`;
    el.style.overflowY = el.scrollHeight + border > max ? "auto" : "hidden";
  }, [autosize, minRows, maxRows]);

  useLayoutEffect(() => {
    resize();
  }, [value, resize]);

  useEffect(() => {
    if (!autosize) return;
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [autosize, resize]);

  const setRef = (el: HTMLTextAreaElement | null) => {
    innerRef.current = el;
    if (typeof ref === "function") ref(el);
    else if (ref) (ref as { current: HTMLTextAreaElement | null }).current = el;
  };

  return (
    <textarea
      ref={setRef}
      rows={autosize ? minRows : props.rows ?? minRows}
      aria-invalid={invalid || undefined}
      value={value}
      onChange={(e) => {
        onChange?.(e);
        if (value === undefined) resize();
      }}
      className={cn(
        controlClass,
        "block px-3 py-2 leading-relaxed",
        autosize && "resize-none",
        mono && "font-mono text-[12.5px]",
        className,
      )}
      {...props}
    />
  );
}

export interface SelectProps extends Omit<ComponentProps<"select">, "size"> {
  options?: { value: string; label: string; disabled?: boolean }[];
  placeholder?: string;
  selectSize?: "sm" | "md";
  invalid?: boolean;
  wrapperClassName?: string;
}

export function Select({
  options,
  placeholder,
  selectSize = "md",
  invalid,
  className,
  wrapperClassName,
  children,
  ...props
}: SelectProps) {
  return (
    <div className={cn("relative", wrapperClassName)}>
      <select
        aria-invalid={invalid || undefined}
        className={cn(
          controlClass,
          "appearance-none pl-3 pr-8",
          selectSize === "sm" ? "h-8 text-[13px]" : "h-9",
          className,
        )}
        {...props}
      >
        {placeholder !== undefined && <option value="">{placeholder}</option>}
        {options?.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-subtle" />
    </div>
  );
}

export function Label({ className, ...props }: ComponentProps<"label">) {
  return <label className={cn("text-[13px] font-medium text-fg", className)} {...props} />;
}

export interface FieldProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  className?: string;
  labelAside?: ReactNode;
  children: ReactNode;
}

/** Label + control + hint/error. Wires `id` / `aria-describedby` onto a single child element. */
export function Field({ label, hint, error, required, className, labelAside, children }: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  let control = children;
  let controlId = id;
  if (isValidElement(children)) {
    const el = children as ReactElement<{ id?: string; "aria-describedby"?: string }>;
    controlId = el.props.id ?? id;
    control = cloneElement(el, {
      id: controlId,
      "aria-describedby": hint || error ? hintId : el.props["aria-describedby"],
    });
  }
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {(label || labelAside) && (
        <div className="flex items-center justify-between gap-2">
          {label && (
            <Label htmlFor={controlId}>
              {label}
              {required && <span className="ml-0.5 text-danger">*</span>}
            </Label>
          )}
          {labelAside}
        </div>
      )}
      {control}
      {error ? (
        <p id={hintId} className="text-xs text-danger">
          {error}
        </p>
      ) : hint ? (
        <p id={hintId} className="text-xs text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
