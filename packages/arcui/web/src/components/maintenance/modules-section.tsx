import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ContextNote } from '@/components/hitl'
import { QueryState, EmptyState } from '@/components/states'
import { RestartStackButton } from '@/components/restart-stack-button'
import { SectionCard } from '@/components/maintenance/section-card'
import { useChangeModule, useMaintenanceModules } from '@/lib/queries'
import { errorText } from '@/lib/maintenance'
import type {
  MaintenanceModuleRow,
  ModuleAction,
  ModuleChangeResponse,
} from '@/lib/types'

type Notice = { tone: 'info' | 'warning'; text: string; restart: boolean }

function StateChip({ enabled }: { enabled: boolean | null }) {
  if (enabled === null) {
    return <span className="text-[11px] text-muted-foreground">Unknown</span>
  }
  return enabled ? (
    <span className="rounded-sm border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400">
      On
    </span>
  ) : (
    <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
      Off
    </span>
  )
}

function ModuleCard({
  row,
  agents,
  editable,
  onResult,
}: {
  row: MaintenanceModuleRow
  agents: { agent_id: string; name: string }[]
  editable: boolean
  onResult: (notice: Notice) => void
}) {
  const change = useChangeModule()

  const run = (agentId: string, action: ModuleAction) =>
    change.mutate(
      { module: row.name, agentId, action },
      {
        onSuccess: (res: ModuleChangeResponse) =>
          onResult({ tone: 'info', text: res.message, restart: res.restart_needed }),
        onError: (e) => onResult({ tone: 'warning', text: errorText(e), restart: false }),
      },
    )

  const staged = row.staged
  const needsBundle = staged !== null && (!row.installed || staged.update_available)

  return (
    <li className="space-y-2 rounded-lg border border-border bg-background p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-mono text-[13px] font-semibold text-foreground">{row.name}</h3>
        {!row.installed && (
          <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
            Not installed
          </span>
        )}
        {staged && (
          <span className="text-[11px] text-muted-foreground">
            {staged.update_available
              ? `Signed bundle ${staged.version ?? ''} is waiting${row.installed ? ' (newer than the one installed)' : ''}`
              : `Signed bundle ${staged.version ?? ''} (already installed)`}
          </span>
        )}
      </div>
      {row.description && <p className="text-xs text-muted-foreground">{row.description}</p>}
      <ul className="divide-y divide-border">
        {agents.map((agent) => {
          const enabled = row.agents[agent.agent_id]?.enabled ?? null
          return (
            <li key={agent.agent_id} className="flex flex-wrap items-center gap-3 py-1.5">
              <span className="min-w-0 flex-1 truncate text-sm text-foreground">{agent.name}</span>
              <StateChip enabled={enabled} />
              {editable && (
                <span className="flex flex-wrap items-center gap-2">
                  {row.installed && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={change.isPending || enabled === null}
                      onClick={() => run(agent.agent_id, enabled ? 'disable' : 'enable')}
                      aria-label={`${enabled ? 'Turn off' : 'Turn on'} ${row.name} for ${agent.name}`}
                    >
                      {enabled ? 'Turn off' : 'Turn on'}
                    </Button>
                  )}
                  {needsBundle && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={change.isPending}
                      onClick={() => run(agent.agent_id, 'install')}
                      aria-label={`${row.installed ? 'Update' : 'Install'} ${row.name} for ${agent.name}`}
                    >
                      {row.installed ? 'Update from bundle' : 'Install from bundle'}
                    </Button>
                  )}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </li>
  )
}

/** Modules and their state for each agent. Only signed bundles install, and the server
 *  checks the signature before it writes anything. */
export function ModulesSection({ editable }: { editable: boolean }) {
  const modules = useMaintenanceModules()
  const [notice, setNotice] = useState<Notice | null>(null)

  return (
    <SectionCard
      title="Modules"
      description="Extra abilities an agent can have, such as web search or a browser. Turn them on or off for each agent. A module only installs from a signed bundle."
    >
      <QueryState
        query={modules}
        isEmpty={(data) => data.modules.length === 0}
        empty={
          <EmptyState
            title="No modules yet"
            description="No module is installed, and no signed bundle is waiting to be installed."
          />
        }
      >
        {(data) => (
          <div className="space-y-3">
            <ul className="space-y-2">
              {data.modules.map((row) => (
                <ModuleCard
                  key={row.name}
                  row={row}
                  agents={data.agents}
                  editable={editable}
                  onResult={setNotice}
                />
              ))}
            </ul>
            {notice && (
              <ContextNote tone={notice.tone}>
                <span role="status">{notice.text}</span>
                {notice.restart && (
                  <span className="ml-2 inline-block align-middle">
                    <RestartStackButton label="Restart Arc" offerDatabases={false} />
                  </span>
                )}
              </ContextNote>
            )}
          </div>
        )}
      </QueryState>
    </SectionCard>
  )
}
