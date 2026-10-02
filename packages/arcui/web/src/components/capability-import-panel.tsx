import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from 'react'
import { AlertTriangle, Archive, CheckCircle2, FileArchive, FolderOpen, ShieldAlert, Upload, XCircle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useRoster } from '@/lib/queries'
import type { Agent } from '@/lib/types'
import { useCapabilityImport, type CapabilityImportReview } from '@/hooks/use-capability-import'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { FieldHelp } from '@/components/help'
import { collectFromDrop, collectFromFileList, packFolder, type PackEntry } from '@/lib/skill-pack'

/** Split a finding's ``code: detail`` wire form for display. */
function splitFinding(finding: string): { code: string; detail: string } {
  const at = finding.indexOf(': ')
  return at < 0 ? { code: finding, detail: '' } : { code: finding.slice(0, at), detail: finding.slice(at + 2) }
}

/** What the operator must read before signing (J4 M4/G10) — never hidden behind a click. */
function ReviewFindings({ findings }: { findings: string[] }) {
  if (findings.length === 0) {
    return <p className="text-xs text-muted-foreground">No review findings.</p>
  }
  return (
    <div className="space-y-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3">
      <p className="flex items-center gap-1.5 text-xs font-medium text-foreground">
        <AlertTriangle className="size-3.5 text-status-warning" /> Review findings — read before promoting
      </p>
      <ul aria-label="Review findings" className="space-y-1">
        {findings.map((finding) => {
          const { code, detail } = splitFinding(finding)
          return (
            <li key={finding} className="flex flex-wrap items-start gap-2 text-xs">
              <Badge variant="outline" className="font-mono">{code}</Badge>
              <span className="min-w-0 break-words text-foreground">{detail}</span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

const SCRIPT_SUFFIXES = ['.py', '.sh', '.js', '.ts', '.rb', '.pl', '.ps1', '.bash']

/** A file that can execute: anything under a ``scripts/`` folder or with a code suffix. */
function isScript(path: string): boolean {
  return path.split('/').includes('scripts') || SCRIPT_SUFFIXES.some((suffix) => path.endsWith(suffix))
}

/** Group reviewed paths by their folder so the pack's shape is visible at a glance. */
function fileTree(paths: string[]): Array<{ folder: string; files: string[] }> {
  const groups = new Map<string, string[]>()
  for (const path of [...paths].sort()) {
    const cut = path.lastIndexOf('/')
    const folder = cut < 0 ? '' : path.slice(0, cut + 1)
    groups.set(folder, [...(groups.get(folder) ?? []), path])
  }
  return [...groups.entries()].map(([folder, files]) => ({ folder, files }))
}

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

  const folderRef = useRef<HTMLInputElement>(null)
  const [packError, setPackError] = useState<string | null>(null)

  // React does not type ``webkitdirectory``; set it on the element directly.
  useEffect(() => {
    folderRef.current?.setAttribute('webkitdirectory', '')
  }, [])

  const choose = (files: FileList | File[]) => {
    setPackError(null)
    const file = files[0]
    if (file) void importer.upload(file)
  }

  // A folder is zipped in the browser and goes through the same upload,
  // so the server applies every intake check to exactly those bytes.
  const chooseFolder = async (entries: PackEntry[]) => {
    setPackError(null)
    try {
      await importer.upload(await packFolder(entries))
    } catch (error) {
      setPackError(error instanceof Error ? `Could not read the folder: ${error.message}` : 'Could not read the folder.')
    }
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
    // Read entries synchronously: the DataTransfer is emptied after this handler.
    const files = Array.from(event.dataTransfer.files ?? [])
    void collectFromDrop(event.dataTransfer.items)
      .then((entries) => (entries ? chooseFolder(entries) : choose(files)))
      .catch((error: unknown) => {
        setPackError(error instanceof Error ? `Could not read the folder: ${error.message}` : 'Could not read the folder.')
      })
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
        <CardDescription>Stage a skill folder, a ZIP or a single SKILL.md for one agent. An operator reviews and signs it before activation.</CardDescription>
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
        <FieldHelp helpKey="capability_import.agent" route="tools-skills" />

        {importer.reviews.length > 0 && (
          <div className="space-y-2 rounded-md border border-border p-3">
            <p className="text-xs font-medium text-foreground">Staged imports</p>
            <div className="flex flex-wrap gap-2">
              {importer.reviews.map((review) => (
                <Button
                  key={review.import_id}
                  type="button"
                  size="sm"
                  variant={importer.review?.import_id === review.import_id ? 'default' : 'outline'}
                  onClick={() => importer.selectReview(review)}
                >
                  {review.skills.concat(review.tools).join(', ') || review.import_id.slice(0, 12)}
                </Button>
              ))}
            </div>
          </div>
        )}

        <div
          role="button"
          tabIndex={0}
          aria-label="Upload a capability folder, ZIP archive or SKILL.md"
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
          <input ref={inputRef} type="file" accept=".zip,.md,application/zip,text/markdown" className="sr-only" onChange={(event) => { choose(event.target.files ?? []); event.currentTarget.value = '' }} />
          {importer.status === 'uploading' ? <Upload className="size-5 animate-pulse text-primary" /> : <FileArchive className="size-5 text-muted-foreground" />}
          <span className="text-sm font-medium text-foreground">{importer.status === 'uploading' ? 'Inspecting source…' : 'Drop a skill folder, ZIP or SKILL.md, or browse'}</span>
          <span className="text-xs text-muted-foreground">Folders and ZIPs can include skill resources. Review does not execute scripts; an operator signs the reviewed source before it becomes available.</span>
        </div>
        <div className="flex items-center gap-2">
          <input
            ref={folderRef}
            id="capability-import-folder"
            type="file"
            multiple
            aria-label="Choose a skill folder"
            className="sr-only"
            onChange={(event) => {
              const entries = collectFromFileList(event.target.files ?? [])
              event.currentTarget.value = ''
              if (entries.length > 0) void chooseFolder(entries)
            }}
          />
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => folderRef.current?.click()}
            disabled={importer.status === 'uploading'}
          >
            <FolderOpen className="size-4" /> Choose folder
          </Button>
        </div>
        <FieldHelp helpKey="capability_import.archive" route="tools-skills" />

        {packError && (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-status-error/30 bg-status-error/10 p-3 text-xs text-status-error">
            <XCircle className="mt-0.5 size-4 shrink-0" /> {packError}
          </div>
        )}
        {importer.status === 'rejected' && (
          <div role="alert" className="flex items-start gap-2 rounded-md border border-status-error/30 bg-status-error/10 p-3 text-xs text-status-error">
            <XCircle className="mt-0.5 size-4 shrink-0" /> {importer.error}
          </div>
        )}
        {importer.review && (
          <>
            <ReviewEvidence review={importer.review} />
            <ReviewFindings findings={importer.review.findings ?? []} />
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
              <FieldHelp helpKey="capability_import.file" route="tools-skills" />
              <ul role="tree" aria-label="Reviewed files" className="space-y-2">
                {fileTree(
                  importer.review.files
                    .map((file) => file.path)
                    .filter((path) => path.startsWith('tools/') || path.startsWith('skills/')),
                ).map(({ folder, files }) => (
                  <li key={folder} role="treeitem" aria-expanded="true" aria-selected="false" className="space-y-1">
                    <p className="font-mono text-xs text-muted-foreground">{folder || '/'}</p>
                    <ul role="group" className="flex flex-wrap gap-2 pl-3">
                      {files.map((path) => (
                        <li key={path} role="treeitem" aria-selected={selectedPath === path}>
                          <Button
                            type="button"
                            size="sm"
                            variant={selectedPath === path ? 'default' : 'outline'}
                            onClick={() => void openFile(path)}
                            title={path}
                          >
                            {path.slice(folder.length)}
                            {isScript(path) && (
                              <Badge variant="secondary" className="ml-1">script</Badge>
                            )}
                          </Button>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
              {selectedPath && (
                <div className="space-y-2">
                  <label className="text-xs text-muted-foreground" htmlFor="capability-import-editor">{selectedPath}</label>
                  <FieldHelp helpKey="capability_import.content" route="tools-skills" />
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
