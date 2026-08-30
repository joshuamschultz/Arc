import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { MarkdownFile } from '@/components/frontmatter'
import { ErrorState, LoadingRows } from '@/components/states'
import { CapabilityStatusBadge, SourceRootBadge } from '@/components/capability-table'
import {
  useAgentSkillDetail,
  useAgentSkillVersionDiff,
  useAgentSkillVersions,
  useRollbackSkill,
} from '@/lib/queries'
import { apiPut, ApiError } from '@/lib/api'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import type { FileWriteResponse, SkillVersionItem } from '@/lib/types'
import { cn } from '@/lib/utils'

/** SKILL.md detail drawer (U5). View mode renders the markdown body;
 *  operator mode adds Edit/Save for workspace/agent-authored skills, saving
 *  through the same `PUT /files/read` route `FileViewer` uses — builtins and
 *  global-root skills stay read-only (no write target from the backend). */
export function SkillDrawer({
  agentId,
  skillName,
  open,
  onOpenChange,
}: {
  agentId: string
  skillName: string | null
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const detail = useAgentSkillDetail(agentId, skillName)
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saveResult, setSaveResult] = useState<FileWriteResponse | null>(null)

  // A newly selected skill always opens in view mode with a clean save state
  // (state adjusted during render, keyed on the selected skill).
  const [prevSkill, setPrevSkill] = useState(skillName)
  if (prevSkill !== skillName) {
    setPrevSkill(skillName)
    setEditing(false)
    setSaveError(null)
    setSaveResult(null)
  }

  if (skillName == null) return null

  const startEdit = () => {
    setDraft(detail.data?.content ?? '')
    setSaveError(null)
    setSaveResult(null)
    setEditing(true)
  }

  const save = async () => {
    const { write_root, write_path } = detail.data ?? {}
    if (!write_root || !write_path) return
    setSaving(true)
    setSaveError(null)
    try {
      const res = await apiPut<FileWriteResponse>(
        `/api/agents/${agentId}/files/read?root=${write_root}&path=${encodeURIComponent(write_path)}`,
        { content: draft },
      )
      setSaveResult(res)
      setEditing(false)
      await queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'skill', skillName] })
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl lg:max-w-3xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">{skillName}</SheetTitle>
          {detail.data && (
            <SheetDescription className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs">{detail.data.version || 'unversioned'}</span>
              <SourceRootBadge value={detail.data.source_root} />
              <CapabilityStatusBadge
                status={detail.data.status}
                detail={detail.data.status_detail}
              />
            </SheetDescription>
          )}
        </SheetHeader>

        <Tabs defaultValue="body" className="flex flex-1 flex-col overflow-hidden">
          <TabsList className="mx-5 mt-3 w-fit">
            <TabsTrigger value="body">Body</TabsTrigger>
            <TabsTrigger value="versions">Versions</TabsTrigger>
          </TabsList>

          <TabsContent value="body" className="flex flex-1 flex-col overflow-hidden">
            <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-2 text-xs text-muted-foreground">
              <span className="truncate rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px]">
                {detail.data?.source_path}
              </span>
              <div className="flex shrink-0 items-center gap-2">
                {operatorMode && detail.data?.editable && !editing && (
                  <Button variant="ghost" size="sm" onClick={startEdit}>
                    <Pencil className="size-3.5" /> Edit
                  </Button>
                )}
                {editing && (
                  <>
                    <Button variant="ghost" size="sm" disabled={saving} onClick={() => setEditing(false)}>
                      Cancel
                    </Button>
                    <Button size="sm" disabled={saving} onClick={save}>
                      {saving ? 'Saving…' : 'Save'}
                    </Button>
                  </>
                )}
              </div>
            </div>

            {saveError && (
              <div className="border-b border-destructive/30 bg-destructive/10 px-5 py-2 text-xs text-destructive">
                {saveError}
              </div>
            )}
            {saveResult?.signature_stale && (
              <div className="border-b border-status-warning/30 bg-status-warning/10 px-5 py-2 text-xs text-status-warning">
                {saveResult.message}
              </div>
            )}
            {saveResult && !saveResult.signature_stale && (
              <div className="border-b border-status-online/30 bg-status-online/10 px-5 py-2 text-xs text-status-online">
                {saveResult.message}
              </div>
            )}

            <div className="flex-1 overflow-auto p-5">
              {detail.isLoading ? (
                <LoadingRows rows={8} />
              ) : detail.isError ? (
                <ErrorState error={detail.error} />
              ) : editing ? (
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  spellCheck={false}
                  className="h-full min-h-[320px] w-full resize-none rounded-md border border-border bg-muted/30 p-3 font-mono text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                />
              ) : (
                <MarkdownFile content={detail.data?.content ?? ''} />
              )}
            </div>
          </TabsContent>

          <TabsContent value="versions" className="flex-1 overflow-auto p-5">
            <SkillVersionsPanel agentId={agentId} skillName={skillName} />
          </TabsContent>
        </Tabs>
      </SheetContent>
    </Sheet>
  )
}

function shortId(id: string): string {
  return id === 'seed' ? 'seed' : id.slice(0, 8)
}

/** One row in the lineage timeline. Click "A"/"B" to pick this candidate as a diff
 *  side; the active candidate can't be rolled back to (it already is the target). */
function VersionRow({
  item,
  isA,
  isB,
  onPickA,
  onPickB,
  onRollback,
  canRollback,
}: {
  item: SkillVersionItem
  isA: boolean
  isB: boolean
  onPickA: () => void
  onPickB: () => void
  onRollback: () => void
  canRollback: boolean
}) {
  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-2 rounded-md border border-border px-3 py-2 text-xs',
        (isA || isB) && 'border-primary/50 bg-primary/5',
      )}
    >
      <span className="font-mono text-[11px] text-foreground">{shortId(item.candidate_id)}</span>
      {item.active && (
        <span className="rounded bg-status-online/15 px-1.5 py-0.5 text-[10px] text-status-online">
          active
        </span>
      )}
      {item.tombstone && (
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          body pruned
        </span>
      )}
      {item.generation != null && (
        <span className="text-muted-foreground">gen {item.generation}</span>
      )}
      {item.parent_id && (
        <span className="text-muted-foreground">← {shortId(item.parent_id)}</span>
      )}
      {item.ts && <span className="text-muted-foreground">{item.ts}</span>}
      <div className="ml-auto flex items-center gap-1">
        <Button
          variant={isA ? 'default' : 'ghost'}
          size="sm"
          className="h-6 px-2 text-[10px]"
          disabled={item.tombstone}
          onClick={onPickA}
        >
          A
        </Button>
        <Button
          variant={isB ? 'default' : 'ghost'}
          size="sm"
          className="h-6 px-2 text-[10px]"
          disabled={item.tombstone}
          onClick={onPickB}
        >
          B
        </Button>
        {canRollback && !item.active && !item.tombstone && (
          <Button variant="outline" size="sm" className="h-6 px-2 text-[10px]" onClick={onRollback}>
            Rollback
          </Button>
        )}
      </div>
    </div>
  )
}

/** Renders a server-computed unified diff with +/- line coloring (plain text parse —
 *  no diff library needed for a format this simple). */
function UnifiedDiff({ diff }: { diff: string }) {
  if (!diff.trim()) {
    return <p className="text-xs text-muted-foreground">No textual difference between these two versions.</p>
  }
  return (
    <pre className="overflow-x-auto rounded-md border border-border bg-muted/20 p-3 font-mono text-[11px] leading-relaxed">
      {diff.split('\n').map((line, i) => (
        <div
          key={i}
          className={cn(
            line.startsWith('+') && !line.startsWith('+++') && 'bg-status-online/10 text-status-online',
            line.startsWith('-') && !line.startsWith('---') && 'bg-destructive/10 text-destructive',
            (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) &&
              'text-muted-foreground',
          )}
        >
          {line || ' '}
        </div>
      ))}
    </pre>
  )
}

/** The reviewable diff-merge surface (H-042): a lineage timeline an operator can
 *  actually inspect — pick two candidates, see the real old→new text diff, then
 *  roll back with the same gated/audited route the timeline already exposed
 *  read-only. Every mutation this produces (prose, code-repair, or a Curator
 *  consolidate) lands here exactly like any other candidate version. */
function SkillVersionsPanel({ agentId, skillName }: { agentId: string; skillName: string }) {
  const [operatorMode] = useOperatorMode()
  const versions = useAgentSkillVersions(agentId, skillName)
  const [sideA, setSideA] = useState<string | null>(null)
  const [sideB, setSideB] = useState<string | null>(null)
  const diff = useAgentSkillVersionDiff(agentId, skillName, sideA, sideB)
  const rollback = useRollbackSkill(agentId, skillName)
  const [rollbackError, setRollbackError] = useState<string | null>(null)

  const doRollback = async (candidateId: string) => {
    if (!window.confirm(`Roll back '${skillName}' to version ${shortId(candidateId)}?`)) return
    setRollbackError(null)
    try {
      await rollback.mutateAsync({ candidateId })
    } catch (e) {
      setRollbackError(e instanceof ApiError ? e.message : 'Rollback failed')
    }
  }

  if (versions.isLoading) return <LoadingRows rows={5} />
  if (versions.isError) return <ErrorState error={versions.error} />

  const items = versions.data?.items ?? []
  if (items.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No version history yet — this skill has not been mutated by the improver.
      </p>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Pick two versions (A/B) to see the actual text change between them before deciding
        whether to roll back.
      </p>
      {rollbackError && <p className="text-xs text-destructive">{rollbackError}</p>}
      {rollback.data && (
        <p className="text-xs text-status-online">{rollback.data.warning}</p>
      )}
      <div className="space-y-1.5">
        {items.map((item) => (
          <VersionRow
            key={item.candidate_id}
            item={item}
            isA={sideA === item.candidate_id}
            isB={sideB === item.candidate_id}
            onPickA={() => setSideA(item.candidate_id)}
            onPickB={() => setSideB(item.candidate_id)}
            onRollback={() => void doRollback(item.candidate_id)}
            canRollback={operatorMode}
          />
        ))}
      </div>

      {sideA && sideB && sideA !== sideB && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-foreground">
            Diff: {shortId(sideA)} → {shortId(sideB)}
          </p>
          {diff.isLoading ? (
            <LoadingRows rows={4} />
          ) : diff.isError ? (
            <ErrorState error={diff.error} />
          ) : (
            <UnifiedDiff diff={diff.data?.diff ?? ''} />
          )}
        </div>
      )}
    </div>
  )
}
