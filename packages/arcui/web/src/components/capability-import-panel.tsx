import { useRef, useState, type DragEvent, type KeyboardEvent } from 'react'
import { Archive, CheckCircle2, FileArchive, ShieldAlert, Upload, XCircle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useRoster } from '@/lib/queries'
import type { Agent } from '@/lib/types'
import { useCapabilityImport, type CapabilityImportReview } from '@/hooks/use-capability-import'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

function ReviewEvidence({ review }: { review: CapabilityImportReview }) {
  const statusLabel = review.status.replaceAll('_', ' ')
  return (
    <div className="space-y-3 rounded-md border border-status-warning/30 bg-status-warning/10 p-3">
      <div className="flex items-start gap-2">
        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-status-online" />
        <div className="min-w-0">
            <p className="text-sm font-medium capitalize text-foreground">{statusLabel}</p>
          <p className="text-xs text-muted-foreground">
            {review.files.length} files · {review.tools.length} tools · {review.skills.length} skills
          </p>
        </div>
      </div>
      <dl className="grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-muted-foreground">Import</dt>
          <dd className="truncate font-mono text-foreground" title={review.import_id}>{review.import_id}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Review digest</dt>
          <dd className="truncate font-mono text-foreground" title={review.review_digest}>{review.review_digest}</dd>
        </div>
      </dl>
      <p className="text-xs text-muted-foreground">
        {review.status === 'promoted'
          ? 'Promoted capabilities are signed and pinned to this agent trust store.'
          : review.status === 'revoked'
            ? 'Revoked capabilities are removed from the active capability roots.'
            : 'This import is quarantined and inactive until an operator promotes the reviewed bytes.'}
      </p>
      {review.status === 'review_ready' && (
        <Button asChild variant="outline" size="sm">
          <Link to="/gated">Open capability trust review</Link>
        </Button>
      )}
    </div>
  )
}

export function CapabilityImportPanel() {
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter(
    (agent): agent is Agent & { agent_id: string } => !agent.hidden && Boolean(agent.agent_id),
  )
  const [agentId, setAgentId] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const importer = useCapabilityImport(agentId)
  const [operatorMode] = useOperatorMode()
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [source, setSource] = useState('')
  const [saving, setSaving] = useState(false)
  const [trusting, setTrusting] = useState(false)

  const choose = (files: FileList | File[]) => {
    const file = files[0]
    if (file) void importer.upload(file)
  }

  const openFile = async (path: string) => {
    if (!importer.review) return
    setSelectedPath(path)
    try {
      const loaded = await importer.readFile(importer.review, path)
      setSource(loaded.content)
    } catch (error) {
      setSource(error instanceof Error ? `Unable to read file: ${error.message}` : 'Unable to read file.')
    }
  }

  const saveFile = async () => {
    if (!importer.review || !selectedPath) return
    setSaving(true)
    try {
      await importer.editFile(importer.review, selectedPath, source)
    } catch (error) {
      setSource(error instanceof Error ? `Unable to save file: ${error.message}` : 'Unable to save file.')
    } finally {
      setSaving(false)
    }
  }

  const promote = async () => {
    if (!importer.review) return
    setTrusting(true)
    try {
      await importer.promote(importer.review)
    } catch (error) {
      setSource(error instanceof Error ? `Unable to promote: ${error.message}` : 'Unable to promote import.')
    } finally {
      setTrusting(false)
    }
  }

  const revoke = async () => {
    if (!importer.review) return
    setTrusting(true)
    try {
      await importer.revoke(importer.review)
    } catch (error) {
      setSource(error instanceof Error ? `Unable to revoke: ${error.message}` : 'Unable to revoke import.')
    } finally {
      setTrusting(false)
    }
  }

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    choose(event.dataTransfer.files)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      inputRef.current?.click()
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base"><Archive className="size-4" /> Import agent capabilities</CardTitle>
        <CardDescription>Drag a signed-source ZIP here to stage skills and tools for one agent.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <label className="flex max-w-sm flex-col gap-1 text-xs font-medium text-muted-foreground" htmlFor="capability-import-agent">
          Target agent
          <select
            id="capability-import-agent"
            className="h-9 rounded-md border border-border bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={agentId ?? ''}
            onChange={(event) => { setAgentId(event.target.value || null); importer.reset() }}
            disabled={roster.isPending || agents.length === 0 || importer.status === 'uploading'}
          >
            <option value="">Choose an agent…</option>
            {agents.map((agent) => <option key={agent.agent_id} value={agent.agent_id}>{agent.display_name || agent.name || agent.agent_id}</option>)}
          </select>
        </label>

        <div
          role="button"
          tabIndex={0}
          aria-label="Upload a capability ZIP archive"
          onClick={() => inputRef.current?.click()}
          onKeyDown={onKeyDown}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true) }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            'flex min-h-28 cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-muted/20 p-5 text-center transition-colors hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/60',
            dragging && 'border-primary bg-primary/10',
            importer.status === 'uploading' && 'pointer-events-none opacity-60',
          )}
        >
          <input ref={inputRef} type="file" accept=".zip,application/zip" className="sr-only" onChange={(event) => { choose(event.target.files ?? []); event.currentTarget.value = '' }} />
          {importer.status === 'uploading' ? <Upload className="size-5 animate-pulse text-primary" /> : <FileArchive className="size-5 text-muted-foreground" />}
          <span className="text-sm font-medium text-foreground">{importer.status === 'uploading' ? 'Inspecting archive…' : 'Drop ZIP or browse'}</span>
          <span className="text-xs text-muted-foreground">Only agent-local skills/tools are accepted; nothing executes during review.</span>
        </div>

        {importer.status === 'rejected' && (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-status-error/30 bg-status-error/10 p-3 text-xs text-status-error">
            <XCircle className="mt-0.5 size-4 shrink-0" /> {importer.error}
          </div>
        )}
        {importer.review && (
          <>
            <ReviewEvidence review={importer.review} />
            {operatorMode && importer.status === 'review_ready' && (
              <Button type="button" size="sm" onClick={() => void promote()} disabled={trusting}>
                {trusting ? 'Promoting…' : 'Promote and sign reviewed import'}
              </Button>
            )}
            {operatorMode && importer.status === 'promoted' && (
              <Button type="button" size="sm" variant="destructive" onClick={() => void revoke()} disabled={trusting}>
                {trusting ? 'Revoking…' : 'Revoke promoted import'}
              </Button>
            )}
            {!operatorMode && importer.status === 'review_ready' && (
              <p className="text-xs text-muted-foreground">Turn on operator controls to promote this import.</p>
            )}
            <div className="space-y-3 rounded-md border border-border p-3">
              <p className="text-xs font-medium text-foreground">Reviewed files</p>
              <div className="flex flex-wrap gap-2">
                {importer.review.files
                  .filter((file) => file.path.startsWith('tools/') || file.path.startsWith('skills/'))
                  .map((file) => (
                    <Button
                      key={file.path}
                      type="button"
                      size="sm"
                      variant={selectedPath === file.path ? 'default' : 'outline'}
                      onClick={() => void openFile(file.path)}
                    >
                      {file.path}
                    </Button>
                  ))}
              </div>
              {selectedPath && (
                <div className="space-y-2">
                  <label className="text-xs text-muted-foreground" htmlFor="capability-import-editor">{selectedPath}</label>
                  <Textarea id="capability-import-editor" value={source} onChange={(event) => setSource(event.target.value)} rows={12} className="font-mono text-xs" />
                  {importer.status === 'review_ready' && (
                    <Button type="button" size="sm" onClick={() => void saveFile()} disabled={saving || !operatorMode}>{saving ? 'Saving review…' : 'Save reviewed edit'}</Button>
                  )}
                </div>
              )}
            </div>
          </>
        )}
        {agents.length === 0 && !roster.isPending && (
          <div className="flex items-start gap-2 text-xs text-muted-foreground"><ShieldAlert className="size-4 shrink-0" /> Register an agent before importing capabilities.</div>
        )}
      </CardContent>
    </Card>
  )
}
