import {
  Boxes,
  MessageSquare,
  Cpu,
  Workflow,
  BookOpen,
  Wrench,
  ListChecks,
  ShieldCheck,
  ShieldAlert,
  Shield,
  PackageCheck,
  Settings,
  type LucideIcon,
} from 'lucide-react'

export interface NavItem {
  /** Path segment under `/` — also the route path. */
  path: string
  label: string
  icon: LucideIcon
  /** Hidden from the sidebar but still routable (e.g. deep-linked detail). */
  hidden?: boolean
}

/**
 * Top-level navigation. Mirrors the package boundary: the old single
 * "Telemetry" page is split into **ArcLLM** (LLM-call layer) and **ArcRun**
 * (agentic-loop layer). Order matches the sidebar.
 */
export const NAV_ITEMS: NavItem[] = [
  { path: 'agents', label: 'Agents', icon: Boxes },
  { path: 'messages', label: 'Messages', icon: MessageSquare },
  { path: 'arcllm', label: 'ArcLLM', icon: Cpu },
  { path: 'arcrun', label: 'ArcRun', icon: Workflow },
  { path: 'knowledge', label: 'Knowledge', icon: BookOpen },
  { path: 'tools-skills', label: 'Tools & Skills', icon: Wrench },
  { path: 'tasks', label: 'Tasks', icon: ListChecks },
  { path: 'approvals', label: 'Approvals', icon: ShieldAlert },
  { path: 'gated', label: 'Gated', icon: PackageCheck },
  { path: 'policy', label: 'Policy', icon: ShieldCheck },
  { path: 'security', label: 'Security', icon: Shield },
  { path: 'settings', label: 'Settings', icon: Settings },
]

export const DEFAULT_PATH = 'agents'
