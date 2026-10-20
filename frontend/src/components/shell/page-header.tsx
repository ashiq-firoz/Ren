"use client";

import { Menu } from "lucide-react";
import type { ReactNode } from "react";
import { IconButton } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { NotificationBell } from "./notification-bell";
import { useShell } from "./shell-context";

export function MobileNavButton({ className }: { className?: string }) {
  const { openMobileNav } = useShell();
  return (
    <IconButton
      label="Open navigation"
      icon={<Menu />}
      size="md"
      onClick={openMobileNav}
      className={cn("min-[900px]:hidden", className)}
    />
  );
}

/** Per-page top bar: serif title, description, actions and the notification bell. */
export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
  className,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <header className={cn("mb-6", className)}>
      <div className="flex items-start gap-3">
        <MobileNavButton className="-ml-1.5 mt-1" />
        <div className="min-w-0 flex-1">
          {eyebrow && <div className="mb-1 text-xs text-muted">{eyebrow}</div>}
          <h1 className="font-serif-display text-[30px] leading-[1.1] text-fg sm:text-[34px]">{title}</h1>
          {description && <p className="mt-1.5 max-w-2xl text-[13.5px] text-muted">{description}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2 pt-1">
          {actions && <div className="hidden items-center gap-2 sm:flex">{actions}</div>}
          <NotificationBell />
        </div>
      </div>
      {actions && <div className="mt-4 flex flex-wrap items-center gap-2 sm:hidden">{actions}</div>}
      {children}
    </header>
  );
}

/** Standard width/padding container for management pages. */
export function PageContainer({ children, className, wide }: { children: ReactNode; className?: string; wide?: boolean }) {
  return (
    <div className={cn("mx-auto w-full px-4 pt-6 pb-16 sm:px-6 lg:px-8", wide ? "max-w-[1440px]" : "max-w-[1200px]", className)}>
      {children}
    </div>
  );
}
