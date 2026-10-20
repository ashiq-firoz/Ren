"use client";

import { LogOut, Monitor, Moon, PanelLeftClose, PanelLeftOpen, Search, Settings, Sun, UserRound } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { LogoMark, Wordmark } from "@/components/brand/logo";
import { Avatar } from "@/components/ui/avatar";
import { Kbd, StatusDot } from "@/components/ui/badge";
import { IconButton } from "@/components/ui/button";
import { DropdownMenu, Tooltip } from "@/components/ui/floating";
import { useApprovals } from "@/hooks/queries";
import { cn } from "@/lib/utils";
import { useAuth, useRole } from "@/providers/auth";
import { useStreamStatus } from "@/providers/stream-provider";
import { useTheme } from "@/providers/theme";
import { isActivePath, NAV_GROUPS } from "./nav-config";
import { useShell } from "./shell-context";
import { ThemeToggle } from "./theme-toggle";
import { WorkspaceSwitcher } from "./workspace-switcher";

function LiveIndicator({ collapsed }: { collapsed?: boolean }) {
  const status = useStreamStatus();
  const tone = status === "open" ? "success" : status === "connecting" || status === "reconnecting" ? "warning" : "idle";
  const label =
    status === "open" ? "Live" : status === "connecting" ? "Connecting…" : status === "reconnecting" ? "Reconnecting…" : "Offline";
  const dot = <StatusDot tone={tone} pulse={status === "open"} size={7} label={`Real-time updates: ${label}`} />;
  if (collapsed)
    return (
      <Tooltip content={`Real-time: ${label}`} placement="right">
        <span className="flex size-8 items-center justify-center">{dot}</span>
      </Tooltip>
    );
  return (
    <div className="flex items-center gap-2 px-2 text-[11px] text-subtle" aria-live="polite">
      {dot}
      <span>{label}</span>
    </div>
  );
}

function UserMenu({ collapsed }: { collapsed?: boolean }) {
  const { user, logout } = useAuth();
  const { theme, setTheme } = useTheme();
  const router = useRouter();
  return (
    <DropdownMenu
      placement={collapsed ? "right-end" : "top-start"}
      className="w-[240px]"
      aria-label="Account"
      header={
        <div className="border-b border-border px-3 py-2.5">
          <p className="truncate text-[13px] font-medium text-fg">{user?.name}</p>
          <p className="truncate text-xs text-muted">{user?.email}</p>
        </div>
      }
      items={[
        { type: "label", label: "Theme" },
        { label: "Beige", icon: <Sun />, checked: theme === "light", onSelect: () => setTheme("light"), keepOpen: true },
        { label: "Dark", icon: <Moon />, checked: theme === "dark", onSelect: () => setTheme("dark"), keepOpen: true },
        { label: "System", icon: <Monitor />, checked: theme === "system", onSelect: () => setTheme("system"), keepOpen: true },
        { type: "separator" },
        { label: "Profile", icon: <UserRound />, onSelect: () => router.push("/settings?tab=profile") },
        { label: "Settings", icon: <Settings />, onSelect: () => router.push("/settings") },
        { type: "separator" },
        { label: "Sign out", icon: <LogOut />, onSelect: logout },
      ]}
      trigger={
        <button
          type="button"
          aria-label="Account menu"
          className={cn(
            "flex w-full items-center gap-2.5 rounded-control text-left transition-colors hover:bg-surface-2",
            collapsed ? "justify-center p-1.5" : "px-2 py-1.5",
          )}
        >
          <Avatar name={user?.name} size="md" />
          {!collapsed && (
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-medium text-fg">{user?.name ?? "—"}</span>
              <span className="block truncate text-[11px] text-muted">{user?.email}</span>
            </span>
          )}
        </button>
      }
    />
  );
}

export function SidebarContent({ collapsed = false, onNavigate }: { collapsed?: boolean; onNavigate?: () => void }) {
  const pathname = usePathname();
  const { isAdmin, mode } = useRole();
  const { setCollapsed, setPaletteOpen } = useShell();
  const pending = useApprovals("pending");
  const pendingCount = pending.data?.items.filter((a) => a.status === "pending").length ?? 0;

  return (
    <div className="flex h-full flex-col">
      <div className={cn("flex h-14 shrink-0 items-center", collapsed ? "justify-center px-2" : "justify-between px-4")}>
        <Link href="/chat" onClick={onNavigate} aria-label="Ren home" className="rounded-md">
          {collapsed ? <LogoMark className="w-9 text-mark" title="Ren by f69 Labs" /> : <Wordmark size="sm" />}
        </Link>
        {!collapsed && onNavigate === undefined && (
          <IconButton
            label="Collapse sidebar"
            icon={<PanelLeftClose />}
            size="xs"
            onClick={() => setCollapsed(true)}
            className="max-[899px]:hidden"
          />
        )}
      </div>

      <div className={cn("shrink-0 pb-2", collapsed ? "px-2" : "px-3")}>
        <WorkspaceSwitcher collapsed={collapsed} />
        {!collapsed ? (
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            className="mt-2 flex h-8 w-full items-center gap-2 rounded-control border border-border bg-surface px-2.5 text-[12.5px] text-subtle transition-colors hover:border-border-strong hover:text-muted"
          >
            <Search className="size-3.5" />
            <span className="flex-1 text-left">Search or jump to…</span>
            <Kbd>⌘K</Kbd>
          </button>
        ) : (
          <Tooltip content="Search (⌘K)" placement="right">
            <button
              type="button"
              aria-label="Search"
              onClick={() => setPaletteOpen(true)}
              className="mt-2 flex size-9 w-full items-center justify-center rounded-control text-muted hover:bg-surface-2 hover:text-fg"
            >
              <Search className="size-4" />
            </button>
          </Tooltip>
        )}
      </div>

      <nav aria-label="Main" className={cn("min-h-0 flex-1 overflow-y-auto pb-3", collapsed ? "px-2" : "px-3")}>
        {NAV_GROUPS.map((group) => {
          const items = group.items.filter((i) => (!i.adminOnly || isAdmin) && (!i.businessOnly || mode === "business"));
          if (items.length === 0) return null;
          return (
            <div key={group.label} className="mt-3 first:mt-1">
              {!collapsed ? (
                <p className="px-2 pb-1 text-[10.5px] font-medium tracking-[0.08em] text-subtle uppercase">{group.label}</p>
              ) : (
                <div className="mx-auto mb-1 h-px w-6 bg-border" />
              )}
              <ul className="space-y-0.5">
                {items.map((item) => {
                  const active = isActivePath(pathname, item.href);
                  const Icon = item.icon;
                  const badge = item.badge === "approvals" && pendingCount > 0 ? pendingCount : null;
                  const link = (
                    <Link
                      href={item.href}
                      onClick={onNavigate}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "group relative flex items-center gap-2.5 rounded-control text-[13px] transition-colors duration-150",
                        collapsed ? "size-9 justify-center" : "h-8 px-2",
                        active ? "bg-surface-2 font-medium text-fg" : "text-muted hover:bg-surface-2/70 hover:text-fg",
                      )}
                    >
                      {active && !collapsed && (
                        <span aria-hidden className="absolute top-1.5 bottom-1.5 -left-3 w-[3px] rounded-r-full bg-focus" />
                      )}
                      <Icon className="size-4 shrink-0" strokeWidth={1.75} />
                      {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
                      {badge !== null &&
                        (collapsed ? (
                          <span className="absolute top-1 right-1 size-2 rounded-full bg-warning ring-2 ring-bg" />
                        ) : (
                          <span className="inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-warning px-1.5 text-[10.5px] font-semibold text-primary-fg">
                            {badge > 99 ? "99+" : badge}
                          </span>
                        ))}
                    </Link>
                  );
                  return (
                    <li key={item.href}>
                      {collapsed ? (
                        <Tooltip content={badge ? `${item.label} (${badge})` : item.label} placement="right">
                          {link}
                        </Tooltip>
                      ) : (
                        link
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </nav>

      <div className={cn("shrink-0 space-y-1 border-t border-border py-2", collapsed ? "px-2" : "px-3")}>
        {collapsed ? (
          <>
            <LiveIndicator collapsed />
            <div className="flex justify-center">
              <ThemeToggle />
            </div>
          </>
        ) : (
          <div className="flex items-center justify-between">
            <LiveIndicator />
            <ThemeToggle size="xs" />
          </div>
        )}
        <UserMenu collapsed={collapsed} />
        {collapsed && (
          <Tooltip content="Expand sidebar" placement="right">
            <button
              type="button"
              aria-label="Expand sidebar"
              onClick={() => setCollapsed(false)}
              className="flex size-9 w-full items-center justify-center rounded-control text-muted hover:bg-surface-2 hover:text-fg"
            >
              <PanelLeftOpen className="size-4" />
            </button>
          </Tooltip>
        )}
      </div>
    </div>
  );
}

export function Sidebar() {
  const { collapsed } = useShell();
  return (
    <aside
      aria-label="Sidebar"
      className={cn(
        "hidden h-dvh shrink-0 border-r border-border bg-bg transition-[width] duration-200 ease-out min-[900px]:block",
        collapsed ? "w-16" : "w-60",
      )}
    >
      <SidebarContent collapsed={collapsed} />
    </aside>
  );
}
