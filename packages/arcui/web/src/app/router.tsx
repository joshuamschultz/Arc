import { createBrowserRouter, Navigate } from 'react-router-dom'
import { AppShell } from '@/components/shell/app-shell'
import { HomePage } from '@/pages/home'
import { AgentsPage } from '@/pages/agents'
import { TasksPage } from '@/pages/tasks'
import { ToolsSkillsPage } from '@/pages/tools-skills'
import { PolicyPage } from '@/pages/policy'
import { SecurityPage } from '@/pages/security'
import { KnowledgePage } from '@/pages/knowledge'
import { SharedKnowledgePage } from '@/pages/shared-knowledge'
import { ArcLlmPage } from '@/pages/arcllm'
import { ArcRunPage } from '@/pages/arcrun'
import { AgentDetailPage } from '@/pages/agent-detail'
import { MessagesPage } from '@/pages/messages'
import { ApprovalsPage } from '@/pages/approvals'
import { GatedCapabilitiesPage } from '@/pages/gated-capabilities'
import { SettingsPage } from '@/pages/settings'
import { WorkflowsPage } from '@/pages/workflows'
import { ConnectionsPage } from '@/pages/connections'
import { LazyWorkflowDetailPage } from './lazy-workflow-detail'
import { DEFAULT_PATH } from './nav'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to={`/${DEFAULT_PATH}`} replace /> },

      // Home / Today — operator daily driver.
      { path: 'home', element: <HomePage /> },

      // Phase 3 — fleet pages.
      { path: 'agents', element: <AgentsPage /> },
      { path: 'tasks', element: <TasksPage /> },
      { path: 'approvals', element: <ApprovalsPage /> },
      { path: 'gated', element: <GatedCapabilitiesPage /> },
      { path: 'tools-skills', element: <ToolsSkillsPage /> },
      { path: 'policy', element: <PolicyPage /> },
      { path: 'security', element: <SecurityPage /> },
      { path: 'knowledge', element: <KnowledgePage /> },
      { path: 'shared-knowledge', element: <SharedKnowledgePage /> },

      // Phase 4–6 — ArcLLM / ArcRun / agent detail.
      { path: 'arcllm', element: <ArcLlmPage /> },
      { path: 'arcrun', element: <ArcRunPage /> },
      { path: 'agents/:id', element: <AgentDetailPage /> },
      { path: 'agents/:id/:tab', element: <AgentDetailPage /> },

      // Phase 7 — Messages + Settings.
      { path: 'messages', element: <MessagesPage /> },
      { path: 'settings', element: <SettingsPage /> },

      // SPEC-061 ArcFlow — workflow management surface (COMP-020).
      { path: 'workflows', element: <WorkflowsPage /> },
      { path: 'workflows/:id', element: <LazyWorkflowDetailPage /> },

      // SPEC-064 — connection surfaces (connectors + keys).
      { path: 'connections', element: <ConnectionsPage /> },

      { path: '*', element: <Navigate to={`/${DEFAULT_PATH}`} replace /> },
    ],
  },
])
