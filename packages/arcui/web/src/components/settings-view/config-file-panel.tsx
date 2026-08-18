import { FileCog } from 'lucide-react'
import { QueryState, EmptyState } from '@/components/states'
import { ContextNote } from '@/components/hitl'
import { useAgentConfigFile, useSystemConfigFile } from '@/lib/queries'
import { ConfigSection } from '@/components/settings-view/config-section'
import { CONFIG_FILE_META } from '@/components/settings-view/config-file-meta'

// One config file, at one scope. Leads with a plain-language summary of what
// the file controls, a note that edits are enforced server-side, then the
// grid of its top-level sections (each independently editable).
export function ConfigFilePanel({
  system,
  agentId,
  file,
  label,
  editable,
}: {
  system: boolean
  agentId: string | null
  file: string
  label: string
  editable: boolean
}) {
  // Both hooks always run (rules of hooks); only the active scope is enabled.
  const agentQuery = useAgentConfigFile(system ? null : agentId, file)
  const systemQuery = useSystemConfigFile(file, system)
  const query = system ? systemQuery : agentQuery
  const endpoint = system
    ? `/api/system-config/${file}`
    : `/api/agents/${agentId}/config/${file}`
  const queryKey = system ? ['system', 'config', file] : ['agent', agentId, 'config', file]
  const scopeLabel = system ? 'System (~/.arc)' : 'this agent'
  const meta = CONFIG_FILE_META[file]
  const Icon = meta?.icon ?? FileCog

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg border border-border bg-muted/40 text-muted-foreground">
          <Icon className="size-4" />
        </span>
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <h2 className="font-display text-[15px] font-bold text-foreground">
              {meta?.title ?? label}
            </h2>
            <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {file}.toml
            </span>
          </div>
          {meta && (
            <p className="mt-0.5 text-[13px] leading-snug text-muted-foreground">{meta.blurb}</p>
          )}
        </div>
      </div>

      <ContextNote tone={editable ? 'info' : 'signed'}>
        {editable ? (
          <>
            Changes are checked and re-signed by the server before they take effect — there is no
            client-side save. Edit one section at a time.
          </>
        ) : (
          <>
            Read-only. Turn on <span className="font-medium">operator controls</span> in the left
            rail to edit. Every saved change is validated and re-signed server-side.
          </>
        )}
      </ContextNote>

      <QueryState
        query={query}
        isEmpty={(data) => !data.sections || Object.keys(data.sections).length === 0}
        empty={
          <EmptyState
            icon={<FileCog className="size-7" />}
            title={`No ${file}.toml`}
            description={`${scopeLabel} has no ${label} config file. There is nothing to edit here yet.`}
          />
        }
      >
        {(data) => (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {Object.entries(data.sections).map(([key, value]) => (
              <ConfigSection
                key={key}
                endpoint={endpoint}
                queryKey={queryKey}
                sectionKey={key}
                value={value}
                editable={editable}
              />
            ))}
          </div>
        )}
      </QueryState>
    </div>
  )
}
