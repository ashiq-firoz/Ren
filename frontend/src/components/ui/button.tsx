"use client";

import { Loader2 } from "lucide-react";
import Link from "next/link";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/utils";

export type ButtonVariant = "primary" | "secondary" | "outline" | "ghost" | "danger" | "danger-outline" | "accent";
export type ButtonSize = "xs" | "sm" | "md" | "lg";

const variants: Record<ButtonVariant, string> = {
  primary: "bg-primary text-primary-fg border border-primary hover:opacity-90 active:opacity-85",
  secondary: "bg-surface text-fg border border-border-strong hover:bg-surface-2 active:bg-surface-3",
  outline: "bg-transparent text-fg border border-border-strong hover:bg-surface-2 active:bg-surface-3",
  ghost: "bg-transparent text-muted border border-transparent hover:text-fg hover:bg-surface-2 active:bg-surface-3",
  danger: "bg-danger text-primary-fg border border-danger hover:opacity-90 active:opacity-85",
  "danger-outline":
    "bg-transparent text-danger border border-[color-mix(in_srgb,var(--danger)_45%,transparent)] hover:bg-[color-mix(in_srgb,var(--danger)_9%,transparent)]",
  accent: "bg-accent text-accent-fg border border-accent hover:opacity-90",
};

const sizes: Record<ButtonSize, string> = {
  xs: "h-7 px-2 text-xs gap-1 rounded-[7px]",
  sm: "h-8 px-3 text-[13px] gap-1.5 rounded-control",
  md: "h-9 px-3.5 text-sm gap-2 rounded-control",
  lg: "h-10 px-4 text-sm gap-2 rounded-control",
};

const base =
  "inline-flex items-center justify-center font-medium whitespace-nowrap select-none transition-[background-color,opacity,color,border-color] duration-150 ease-out disabled:opacity-50 disabled:pointer-events-none [&_svg]:shrink-0";

export interface ButtonProps extends ComponentProps<"button"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: ReactNode;
  iconRight?: ReactNode;
}

export function buttonClass(variant: ButtonVariant = "secondary", size: ButtonSize = "md", className?: string) {
  return cn(base, variants[variant], sizes[size], className);
}

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  icon,
  iconRight,
  className,
  children,
  disabled,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={buttonClass(variant, size, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <Spinner size={size === "xs" ? 12 : 14} /> : icon}
      {children}
      {iconRight}
    </button>
  );
}

export interface ButtonLinkProps extends ComponentProps<typeof Link> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
  iconRight?: ReactNode;
}

export function ButtonLink({ variant = "secondary", size = "md", icon, iconRight, className, children, ...props }: ButtonLinkProps) {
  return (
    <Link className={buttonClass(variant, size, className)} {...props}>
      {icon}
      {children}
      {iconRight}
    </Link>
  );
}

export type IconButtonSize = "xs" | "sm" | "md";

const iconSizes: Record<IconButtonSize, string> = {
  xs: "size-6 rounded-md",
  sm: "size-7 rounded-[7px]",
  md: "size-8 rounded-control",
};

export interface IconButtonProps extends Omit<ComponentProps<"button">, "children"> {
  label: string;
  icon: ReactNode;
  variant?: "ghost" | "outline" | "secondary" | "primary" | "danger";
  size?: IconButtonSize;
  loading?: boolean;
  active?: boolean;
}

export function IconButton({
  label,
  icon,
  variant = "ghost",
  size = "sm",
  loading,
  active,
  className,
  type = "button",
  disabled,
  ...props
}: IconButtonProps) {
  const v =
    variant === "ghost"
      ? cn(
          "text-muted hover:text-fg hover:bg-surface-2 border border-transparent",
          active && "bg-surface-2 text-fg",
        )
      : variant === "outline"
        ? "text-fg border border-border-strong hover:bg-surface-2"
        : variant === "secondary"
          ? "text-fg bg-surface border border-border-strong hover:bg-surface-2"
          : variant === "primary"
            ? "bg-primary text-primary-fg border border-primary hover:opacity-90"
            : "text-danger border border-transparent hover:bg-[color-mix(in_srgb,var(--danger)_10%,transparent)]";
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      disabled={disabled || loading}
      className={cn(
        "inline-flex shrink-0 items-center justify-center transition-colors duration-150 disabled:opacity-40 disabled:pointer-events-none [&_svg]:size-4",
        iconSizes[size],
        v,
        className,
      )}
      {...props}
    >
      {loading ? <Spinner size={14} /> : icon}
    </button>
  );
}

export function Spinner({ size = 16, className }: { size?: number; className?: string }) {
  return <Loader2 aria-hidden style={{ width: size, height: size }} className={cn("spin shrink-0", className)} />;
}
