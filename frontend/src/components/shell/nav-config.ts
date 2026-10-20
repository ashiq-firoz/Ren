import {
  Activity,
  BookOpen,
  Bot,
  Brain,
  CalendarClock,
  FolderOpen,
  LayoutDashboard,
  ListChecks,
  MessageSquare,
  Plug,
  Scale,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  badge?: "approvals";
  adminOnly?: boolean;
  businessOnly?: boolean;
  description: string;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Work",
    items: [
      { href: "/overview", label: "Overview", icon: LayoutDashboard, description: "Live swarm, KPIs and recent work" },
      { href: "/chat", label: "Chat", icon: MessageSquare, description: "Talk to Ren" },
      { href: "/tasks", label: "Tasks", icon: ListChecks, description: "Everything the swarm is working on" },
      { href: "/schedules", label: "Schedules", icon: CalendarClock, description: "Recurring and one-off work" },
      { href: "/outputs", label: "Outputs", icon: FolderOpen, description: "Files your agents produced" },
      { href: "/approvals", label: "Approvals", icon: ShieldCheck, badge: "approvals", description: "Actions waiting for you" },
    ],
  },
  {
    label: "Swarm",
    items: [
      { href: "/agents", label: "Agents", icon: Bot, description: "Specialists in your swarm" },
      { href: "/skills", label: "Skills", icon: Sparkles, description: "Reusable playbooks" },
      { href: "/knowledge", label: "Knowledge", icon: BookOpen, description: "Documents and retrieval" },
      { href: "/connectors", label: "Connectors", icon: Plug, description: "Tools, integrations and MCP" },
    ],
  },
  {
    label: "Governance",
    items: [
      { href: "/policies", label: "Policies", icon: Scale, description: "Alignment rules and checks" },
      { href: "/memory", label: "Memory", icon: Brain, description: "What Ren remembers" },
      { href: "/activity", label: "Activity", icon: Activity, adminOnly: true, description: "Audit log" },
    ],
  },
  {
    label: "Workspace",
    items: [
      { href: "/team", label: "Team", icon: Users, businessOnly: true, description: "Members and invites" },
      { href: "/settings", label: "Settings", icon: Settings, description: "Workspace, models and preferences" },
    ],
  },
];

export function isActivePath(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}
