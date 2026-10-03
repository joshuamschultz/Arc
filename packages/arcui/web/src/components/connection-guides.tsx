import { useState } from 'react'
import { BookOpen, Eye, History, Pencil, ShieldCheck, TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { EmptyState, QueryState } from '@/components/states'
import { Markdown } from '@/components/markdown'
import { isDatastoreKind } from '@/lib/connection-kind'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { readTables, setField } from '@/lib/semantic-layer-toml'
import {
  useAgentConnectors,
  useConnectedSources,
  useConnectionGuide,
  useConnectionGuideHistory,
  useConnectionGuideStarter,
  useRestoreConnectionGuide,
  useSaveConnectionGuide,
  useSaveSemanticLayer,
  useSemanticLayer,
} from '@/lib/queries'
import type { AgentConnectorInstance, ConnectedSourceItem } from '@/lib/types'

/** The most a guide may hold: the same ceiling the server enforces. */
export const GUIDE_LIMIT_BYTES = 16 * 1024

const SECTION_HEADING = 'text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground'

/** What a person calls a connection: "Confluence", or "Google (work)" when the
 *  instance is not just the extension itself. */
function connectionName(c: AgentConnectorInstance): string {
  const base = c.extension_display_name || c.extension
  return c.instance === c.extension || c.instance === base ? base : `${base} (${c.instance})`
}

function sourceFor(c: AgentConnectorInstance, sources: ConnectedSourceItem[]) {
  return sources.find((s) => s.connection_id === c.instance || s.connection_id.startsWith(`${c.instance}:`))
}

const byteLength = (text: string) => new TextEncoder().encode(text).length

const errorText = (e: Error) => e.message || 'The request failed.'

// --- Navigation guide --------------------------------------------------------

function GuideHistory({ instance, operatorMode, onRestored }: { instance: string; operatorMode: boolean; onRestored: () => void }) {
  const history = useConnectionGuideHistory(instance, true)
  const restore = useRestoreConnectionGuide(instance)
  return (
    <div className="space-y-2 rounded-md border border-border bg-muted/20 p-3">
      <QueryState
        query={history}
        isEmpty={(d) => (d.versions ?? []).length === 0}
        empty={<p className="text-xs text-muted-foreground">No saved versions yet.</p>}
      >
        {(data) => (
          <ul className="space-y-1.5">
            {data.versions.map((v) => (
              <li key={v.version} className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span className="min-w-0 break-words text-foreground">
                  Version {v.version}
                  <span className="text-muted-foreground">
                    {' '}
                    · {v.updated_at ?? 'unknown time'} · {v.signer ?? 'unsigned'}
                  </span>
                </span>
                {operatorMode && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={restore.isPending}
                    aria-label={`Restore version ${v.version}`}
                    onClick={() => restore.mutate(v.version, { onSuccess: onRestored })}
                  >
                    Restore
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </QueryState>
      {restore.isError && (
        <p role="alert" className="text-xs text-destructive">
          {errorText(restore.error)}
        </p>
      )}
    </div>
  )
}

function NavigationGuide({ instance }: { instance: string }) {
  const [operatorMode] = useOperatorMode()
  const guide = useConnectionGuide(instance)
  const save = useSaveConnectionGuide(instance)
  const starter = useConnectionGuideStarter(instance)
  const [edited, setEdited] = useState<string | null>(null)
  const [preview, setPreview] = useState(false)
  const [showHistory, setShowHistory] = useState(false)

  return (
    <section className="space-y-2">
      <h4 className={SECTION_HEADING}>Navigation guide</h4>
      <p className="text-xs text-muted-foreground">
        Agents read this to find their way around this source. Only signed guides are used.
      </p>
      <QueryState query={guide}>
        {(data) => {
          const value = edited ?? data.content
          const size = byteLength(value)
          const tooBig = size > GUIDE_LIMIT_BYTES
          const empty = value.trim() === ''
          return (
            <div className="space-y-2">
              {data.tampered && (
                <p role="alert" className="flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                  <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
                  This guide was changed outside Arc and is not being used. Save it again to re-sign.
                </p>
              )}
              {preview ? (
                <div className="min-h-24 rounded-md border border-border bg-muted/20 p-3 text-sm" data-testid="guide-preview">
                  {empty ? <p className="text-xs text-muted-foreground">Nothing to preview yet.</p> : <Markdown>{value}</Markdown>}
                </div>
              ) : (
                <Textarea
                  aria-label="Navigation guide"
                  rows={8}
                  value={value}
                  onChange={(e) => setEdited(e.target.value)}
                  placeholder="Describe where things live in this source and how an agent should look for them."
                  className="max-h-96 min-h-40 w-full font-mono text-xs"
                />
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" variant="outline" aria-pressed={preview} onClick={() => setPreview((p) => !p)}>
                  {preview ? <Pencil className="size-3.5" /> : <Eye className="size-3.5" />}
                  {preview ? 'Edit' : 'Preview'}
                </Button>
                {empty && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={starter.isPending}
                    onClick={() => starter.mutate(undefined, { onSuccess: (s) => setEdited(s.content) })}
                  >
                    <BookOpen className="size-3.5" /> Start from a draft
                  </Button>
                )}
                <Button size="sm" variant="outline" aria-expanded={showHistory} onClick={() => setShowHistory((s) => !s)}>
                  <History className="size-3.5" /> History
                </Button>
                {operatorMode && (
                  <Button
                    size="sm"
                    disabled={save.isPending || tooBig || edited === null}
                    onClick={() => save.mutate(value, { onSuccess: () => setEdited(null) })}
                  >
                    <ShieldCheck className="size-3.5" /> Save and sign
                  </Button>
                )}
                <span className={tooBig ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
                  {(size / 1024).toFixed(1)} KB of 16 KB
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                {data.signed && !data.tampered
                  ? `Signed by ${data.signer ?? 'the operator'}${data.updated_at ? ` on ${data.updated_at}` : ''}.`
                  : 'Not signed yet, so agents are not using it.'}
              </p>
              {tooBig && (
                <p role="alert" className="text-xs text-destructive">
                  This guide is over 16 KB. Shorten it to save.
                </p>
              )}
              {(save.isError || starter.isError) && (
                <p role="alert" className="text-xs text-destructive">
                  {errorText((save.error ?? starter.error) as Error)}
                </p>
              )}
              {showHistory && <GuideHistory instance={instance} operatorMode={operatorMode} onRestored={() => setEdited(null)} />}
            </div>
          )
        }}
      </QueryState>
    </section>
  )
}

// --- Table meanings ----------------------------------------------------------

function TableMeanings({ instance }: { instance: string }) {
  const [operatorMode] = useOperatorMode()
  const layer = useSemanticLayer(instance)
  const save = useSaveSemanticLayer(instance)
  const [edited, setEdited] = useState<string | null>(null)
  const [raw, setRaw] = useState(false)

  return (
    <section className="space-y-2">
      <h4 className={SECTION_HEADING}>Table meanings</h4>
      <p className="text-xs text-muted-foreground">
        Say what each table and column means in plain words. Agents read this before they query the data.
      </p>
      <QueryState query={layer}>
        {(data) => {
          const content = edited ?? data.content
          const tables = readTables(content)
          return (
            <div className="space-y-3">
              {raw ? (
                <Textarea
                  aria-label="Table meanings as TOML"
                  rows={10}
                  value={content}
                  onChange={(e) => setEdited(e.target.value)}
                  className="max-h-96 min-h-40 w-full font-mono text-xs"
                />
              ) : tables.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  No tables are described yet. They appear after Arc first reads this database.
                </p>
              ) : (
                <ul className="space-y-3">
                  {tables.map((t) => (
                    <li key={t.name} className="min-w-0 space-y-2 rounded-lg border border-border bg-muted/20 p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="min-w-0 break-all font-mono text-xs text-muted-foreground">{t.name}</span>
                        <label className="flex items-center gap-2 text-xs">
                          <input
                            type="checkbox"
                            className="size-4 accent-primary"
                            checked={t.hidden}
                            aria-label={`Hide table ${t.name}`}
                            onChange={(e) => setEdited(setField(content, { table: t.name, column: null }, 'hidden', e.target.checked))}
                          />
                          Hidden from agents
                        </label>
                      </div>
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        <Input
                          aria-label={`Display name for table ${t.name}`}
                          placeholder="Display name"
                          value={t.entity}
                          className="w-full min-w-0"
                          onChange={(e) => setEdited(setField(content, { table: t.name, column: null }, 'entity', e.target.value))}
                        />
                        <Input
                          aria-label={`Description for table ${t.name}`}
                          placeholder="What is in this table?"
                          value={t.description}
                          className="w-full min-w-0"
                          onChange={(e) => setEdited(setField(content, { table: t.name, column: null }, 'description', e.target.value))}
                        />
                      </div>
                      {t.columns.length > 0 && (
                        <details className="text-xs">
                          <summary className="cursor-pointer text-muted-foreground">
                            {t.columns.length} column{t.columns.length === 1 ? '' : 's'}
                          </summary>
                          <ul className="mt-2 space-y-2">
                            {t.columns.map((c) => (
                              <li key={c.name} className="grid grid-cols-1 gap-2 sm:grid-cols-[8rem_1fr_1fr] sm:items-center">
                                <span className="min-w-0 break-all font-mono text-muted-foreground">{c.name}</span>
                                <Input
                                  aria-label={`Display name for column ${t.name}.${c.name}`}
                                  placeholder="Display name"
                                  value={c.label}
                                  className="w-full min-w-0"
                                  onChange={(e) => setEdited(setField(content, { table: t.name, column: c.name }, 'label', e.target.value))}
                                />
                                <Input
                                  aria-label={`Description for column ${t.name}.${c.name}`}
                                  placeholder="What does it hold?"
                                  value={c.description}
                                  className="w-full min-w-0"
                                  onChange={(e) => setEdited(setField(content, { table: t.name, column: c.name }, 'description', e.target.value))}
                                />
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-2 text-xs">
                  <input type="checkbox" className="size-4 accent-primary" checked={raw} onChange={(e) => setRaw(e.target.checked)} />
                  Edit as raw TOML
                </label>
                {operatorMode && (
                  <Button
                    size="sm"
                    disabled={save.isPending || edited === null}
                    onClick={() => save.mutate(content, { onSuccess: () => setEdited(null) })}
                  >
                    <ShieldCheck className="size-3.5" /> Save table meanings
                  </Button>
                )}
                {data.signed && edited === null && <span className="text-xs text-muted-foreground">Signed.</span>}
              </div>
              {save.isError && (
                <p role="alert" className="text-xs text-destructive">
                  {errorText(save.error)}
                </p>
              )}
              {save.isSuccess && edited === null && <p className="text-xs text-status-success">Saved and signed.</p>}
            </div>
          )
        }}
      </QueryState>
    </section>
  )
}

// --- The Guides tab ----------------------------------------------------------

function GuideCard({ connection, kind, datastore }: { connection: AgentConnectorInstance; kind: string; datastore: boolean }) {
  return (
    <li data-testid={`guide-card-${connection.instance}`} className="min-w-0 max-w-full space-y-4 rounded-lg border border-border bg-card p-3.5">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h3 className="text-sm font-semibold text-foreground">{connectionName(connection)}</h3>
        <span className="text-xs text-muted-foreground">{kind}</span>
      </div>
      <NavigationGuide instance={connection.instance} />
      {datastore && <TableMeanings instance={connection.instance} />}
    </li>
  )
}

/** One card per connection this agent is granted: the guide agents use to find their
 *  way around it, and for a database, what its tables and columns mean. */
export function GuidesSection({ agentId }: { agentId: string }) {
  const connectors = useAgentConnectors(agentId)
  const sources = useConnectedSources(agentId)
  const items = sources.data?.items ?? []
  return (
    <QueryState
      query={connectors}
      isEmpty={(d) => d.instances.length === 0}
      empty={
        <EmptyState
          icon={<BookOpen className="size-5" />}
          title="No connections granted"
          description="Grant this agent a connection, then write a guide for it here."
        />
      }
    >
      {(data) => (
        <ul className="flex flex-col gap-3">
          {data.instances.map((c) => {
            const source = sourceFor(c, items)
            const kindText = source?.source_kind ?? c.extension
            return (
              <GuideCard
                key={c.instance}
                connection={c}
                kind={kindText.replace(/_/g, ' ')}
                datastore={isDatastoreKind(kindText) || isDatastoreKind(c.extension)}
              />
            )
          })}
        </ul>
      )}
    </QueryState>
  )
}
