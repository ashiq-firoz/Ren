"use client";

import {
  Activity,
  BarChart3,
  BookOpen,
  Bot,
  Brain,
  Briefcase,
  Building2,
  Calculator,
  Calendar,
  ChartLine,
  ChartPie,
  ClipboardList,
  Cloud,
  Code,
  Compass,
  Cpu,
  Database,
  DollarSign,
  FileSearch,
  FileSpreadsheet,
  FileText,
  Folder,
  GitBranch,
  Globe,
  Hammer,
  Handshake,
  Headphones,
  Inbox,
  Landmark,
  Layers,
  Lightbulb,
  Link,
  ListChecks,
  Lock,
  Mail,
  Megaphone,
  MessageSquare,
  Network,
  Newspaper,
  Notebook,
  Palette,
  PenLine,
  PenTool,
  Plug,
  Presentation,
  Puzzle,
  Rocket,
  Scale,
  ScanSearch,
  Search,
  Send,
  Server,
  Settings,
  Shield,
  ShieldCheck,
  Sparkles,
  SquareKanban,
  Target,
  Telescope,
  Terminal,
  TrendingUp,
  Users,
  Wallet,
  Workflow,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { DynamicIcon, dynamicIconImports, type IconName } from "lucide-react/dynamic";
import type { CSSProperties } from "react";
import { cn } from "@/lib/utils";

/** Rendered synchronously (no async flash) — the names our seeds most likely use. */
const STATIC: Record<string, LucideIcon> = {
  activity: Activity,
  "bar-chart-3": BarChart3,
  "bar-chart": BarChart3,
  "chart-bar": BarChart3,
  "book-open": BookOpen,
  bot: Bot,
  brain: Brain,
  briefcase: Briefcase,
  "building-2": Building2,
  calculator: Calculator,
  calendar: Calendar,
  "chart-line": ChartLine,
  "line-chart": ChartLine,
  "chart-pie": ChartPie,
  "pie-chart": ChartPie,
  "clipboard-list": ClipboardList,
  cloud: Cloud,
  code: Code,
  compass: Compass,
  cpu: Cpu,
  database: Database,
  "dollar-sign": DollarSign,
  "file-search": FileSearch,
  "file-spreadsheet": FileSpreadsheet,
  "file-text": FileText,
  folder: Folder,
  "git-branch": GitBranch,
  globe: Globe,
  hammer: Hammer,
  handshake: Handshake,
  headphones: Headphones,
  inbox: Inbox,
  landmark: Landmark,
  layers: Layers,
  lightbulb: Lightbulb,
  link: Link,
  "list-checks": ListChecks,
  lock: Lock,
  mail: Mail,
  megaphone: Megaphone,
  "message-square": MessageSquare,
  network: Network,
  newspaper: Newspaper,
  notebook: Notebook,
  palette: Palette,
  "pen-line": PenLine,
  "pen-tool": PenTool,
  plug: Plug,
  presentation: Presentation,
  puzzle: Puzzle,
  rocket: Rocket,
  scale: Scale,
  "scan-search": ScanSearch,
  search: Search,
  send: Send,
  server: Server,
  settings: Settings,
  shield: Shield,
  "shield-check": ShieldCheck,
  sparkles: Sparkles,
  "square-kanban": SquareKanban,
  kanban: SquareKanban,
  target: Target,
  telescope: Telescope,
  terminal: Terminal,
  "trending-up": TrendingUp,
  users: Users,
  wallet: Wallet,
  workflow: Workflow,
  wrench: Wrench,
  zap: Zap,
};

/** Brand / product names lucide doesn't ship → a sensible generic glyph. */
const ALIASES: Record<string, string> = {
  github: "git-branch",
  gitlab: "git-merge",
  bitbucket: "git-fork",
  slack: "hash",
  discord: "message-circle",
  teams: "users",
  linkedin: "briefcase",
  twitter: "at-sign",
  x: "at-sign",
  gmail: "mail",
  email: "mail",
  imap: "mail",
  smtp: "send",
  google: "globe",
  "google-calendar": "calendar",
  "google-drive": "hard-drive",
  drive: "hard-drive",
  notion: "notebook",
  confluence: "book-open",
  jira: "square-kanban",
  linear: "list-checks",
  trello: "square-kanban",
  asana: "list-checks",
  hubspot: "users",
  salesforce: "cloud",
  aws: "cloud",
  azure: "cloud",
  gcp: "cloud",
  microsoft: "layout-grid",
  "microsoft-365": "layout-grid",
  office: "layout-grid",
  outlook: "mail",
  postgres: "database",
  postgresql: "database",
  mysql: "database",
  sql: "database",
  figma: "pen-tool",
  stripe: "credit-card",
  shopify: "shopping-bag",
  zendesk: "headphones",
  mcp: "plug",
  browser: "globe",
  web: "globe",
  python: "code",
  shell: "terminal",
  orchestrator: "sparkles",
};

function normalize(name?: string | null): string {
  if (!name) return "";
  const kebab = name
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .replace(/([A-Z])([A-Z][a-z])/g, "$1-$2")
    .replace(/[\s_]+/g, "-")
    .toLowerCase();
  return ALIASES[kebab] ?? kebab;
}

export interface AppIconProps {
  name?: string | null;
  className?: string;
  style?: CSSProperties;
  strokeWidth?: number;
  /** Used when `name` is missing or unknown. */
  fallback?: string;
}

/** Render a lucide icon by (kebab-case) name, as sent by the API for agents, skills and tool groups. */
export function AppIcon({ name, className, style, strokeWidth = 1.75, fallback = "bot" }: AppIconProps) {
  const n = normalize(name);
  const Static = STATIC[n];
  if (Static) return <Static aria-hidden className={cn("shrink-0", className)} style={style} strokeWidth={strokeWidth} />;
  if (n && n in dynamicIconImports) {
    // The wrapper reserves the icon box while the icon module loads.
    return (
      <span aria-hidden className={cn("inline-flex shrink-0", className)} style={style}>
        <DynamicIcon name={n as IconName} className="size-full" strokeWidth={strokeWidth} />
      </span>
    );
  }
  const Fallback = STATIC[normalize(fallback)] ?? Bot;
  return <Fallback aria-hidden className={cn("shrink-0", className)} style={style} strokeWidth={strokeWidth} />;
}
