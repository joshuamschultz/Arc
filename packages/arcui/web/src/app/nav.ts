import {
  Boxes,
  MessageSquare,
  Cpu,
  Workflow,
  GitBranch,
  BookOpen,
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

/** Rail groups, in order. Keeps 14 destinations legible as five clusters. */
export const NAV_GROUPS = ['work', 'watch', 'govern', 'build', 'system'] as const
export type NavGroup = (typeof NAV_GROUPS)[number]

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
 * Top-level navigation, grouped for the slim rail: work you do with agents,
 * things you watch, things you govern, capabilities you build on, and system.
 * Routes are unchanged; labels use the operator's vocabulary.
 */
export const NAV_ITEMS: NavItem[] = [
  { path: 'agents', label: 'Fleet', icon: Boxes, group: 'work' },
  { path: 'messages', label: 'Chat', icon: MessageSquare, group: 'work' },
  { path: 'tasks', label: 'Tasks', icon: ListChecks, group: 'work' },
  { path: 'workflows', label: 'Workflows', icon: GitBranch, group: 'work' },

  { path: 'arcrun', label: 'Activity', icon: Workflow, group: 'watch' },
  { path: 'arcllm', label: 'ArcLLM', icon: Cpu, group: 'watch' },
  { path: 'knowledge', label: 'Knowledge', icon: BookOpen, group: 'watch' },
  { path: 'security', label: 'Audit', icon: Shield, group: 'watch' },

  { path: 'approvals', label: 'Approvals', icon: ShieldAlert, group: 'govern' },
  { path: 'gated', label: 'Gated', icon: PackageCheck, group: 'govern' },
  { path: 'policy', label: 'Policy', icon: ShieldCheck, group: 'govern' },

  { path: 'tools-skills', label: 'Tools & Skills', icon: Wrench, group: 'build' },
  { path: 'connections', label: 'Connections', icon: Plug, group: 'build' },

  { path: 'settings', label: 'Settings', icon: Settings, group: 'system' },
]

export const DEFAULT_PATH = 'agents'
