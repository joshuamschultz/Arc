import {
  Home,
  Boxes,
  MessageSquare,
  Cpu,
  Workflow,
  GitBranch,
  BookOpen,
  Share2,
  Wrench,
  Plug,
  ListChecks,
  ShieldCheck,
  ShieldAlert,
  Shield,
  PackageCheck,
  Settings,
  type LucideIcon,
} from 'lucide-react'

/** Rail groups, in order. Business-first: what you do, what you govern, what
 *  you watch, then developer/advanced surfaces, then system. */
export const NAV_GROUPS = ['work', 'govern', 'watch', 'advanced', 'system'] as const
export type NavGroup = (typeof NAV_GROUPS)[number]

/** Section headers shown when the rail is expanded. Collapsed, the groups are
 *  read as hairline-separated clusters instead. */
export const GROUP_LABELS: Record<NavGroup, string> = {
  work: 'Work',
  govern: 'Govern',
  watch: 'Observe',
  advanced: 'Advanced',
  system: 'System',
}

export interface NavItem {
  /** Path segment under `/` — also the route path. */
  path: string
  label: string
  icon: LucideIcon
  group: NavGroup
  /** Hidden from the sidebar but still routable (e.g. deep-linked detail). */
  hidden?: boolean
}

/**
 * Top-level navigation for a business operator. Labels use plain language;
 * developer telemetry (model usage, tool wiring) sits under "advanced". Routes
 * are unchanged — only labels and grouping. Technical ids live in detail views.
 */
export const NAV_ITEMS: NavItem[] = [
  { path: 'home', label: 'Home', icon: Home, group: 'work' },
  { path: 'agents', label: 'Fleet', icon: Boxes, group: 'work' },
  { path: 'messages', label: 'Chat', icon: MessageSquare, group: 'work' },
  { path: 'tasks', label: 'Tasks', icon: ListChecks, group: 'work' },

  { path: 'approvals', label: 'Approvals', icon: ShieldAlert, group: 'govern' },
  { path: 'gated', label: 'Pending capabilities', icon: PackageCheck, group: 'govern' },
  { path: 'policy', label: 'Rules', icon: ShieldCheck, group: 'govern' },
  { path: 'security', label: 'Audit', icon: Shield, group: 'govern' },

  { path: 'arcrun', label: 'Activity', icon: Workflow, group: 'watch' },
  { path: 'workflows', label: 'Workflows', icon: GitBranch, group: 'watch' },
  { path: 'knowledge', label: 'Knowledge', icon: BookOpen, group: 'watch' },
  { path: 'shared-knowledge', label: 'Shared knowledge', icon: Share2, group: 'watch' },

  { path: 'arcllm', label: 'Model usage', icon: Cpu, group: 'advanced' },
  { path: 'tools-skills', label: 'Tools & Skills', icon: Wrench, group: 'advanced' },
  { path: 'connections', label: 'Connections', icon: Plug, group: 'advanced' },

  { path: 'settings', label: 'Settings', icon: Settings, group: 'system' },
]

export const DEFAULT_PATH = 'home'
