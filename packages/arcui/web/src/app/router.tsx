import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AppShell } from '@/components/shell/app-shell'
import { AgentsPage } from '@/pages/agents'
import { TasksPage } from '@/pages/tasks'
import { ToolsSkillsPage } from '@/pages/tools-skills'
import { PolicyPage } from '@/pages/policy'
import { SecurityPage } from '@/pages/security'
import { KnowledgePage } from '@/pages/knowledge'
import { ArcLlmPage } from '@/pages/arcllm'
import { ArcRunPage } from '@/pages/arcrun'
import { AgentDetailPage } from '@/pages/agent-detail'
import { MessagesPage } from '@/pages/messages'
import { ApprovalsPage } from '@/pages/approvals'
import { GatedCapabilitiesPage } from '@/pages/gated-capabilities'
import { SettingsPage } from '@/pages/settings'
import { DEFAULT_PATH } from './nav'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to={`/${DEFAULT_PATH}`} replace /> },

      // Phase 3 — fleet pages.
      { path: 'agents', element: <AgentsPage /> },
      { path: 'tasks', element: <TasksPage /> },
      { path: 'approvals', element: <ApprovalsPage /> },
      { path: 'gated', element: <GatedCapabilitiesPage /> },
      { path: 'tools-skills', element: <ToolsSkillsPage /> },
      { path: 'policy', element: <PolicyPage /> },
      { path: 'security', element: <SecurityPage /> },
      { path: 'knowledge', element: <KnowledgePage /> },

      // Phase 4–6 — ArcLLM / ArcRun / agent detail.
      { path: 'arcllm', element: <ArcLlmPage /> },
      { path: 'arcrun', element: <ArcRunPage /> },
      { path: 'agents/:id', element: <AgentDetailPage /> },
      { path: 'agents/:id/:tab', element: <AgentDetailPage /> },

      // Phase 7 — Messages + Settings.
      { path: 'messages', element: <MessagesPage /> },
      { path: 'settings', element: <SettingsPage /> },

      { path: '*', element: <Navigate to={`/${DEFAULT_PATH}`} replace /> },
    ],
  },
])
