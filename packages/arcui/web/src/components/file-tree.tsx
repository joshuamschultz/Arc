import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, File, Folder, FolderOpen, Pencil } from 'lucide-react'
import { apiGet, apiPut, ApiError } from '@/lib/api'
import type { FileReadResponse, FilesTreeEntry, FilesTreeResponse, FileWriteResponse } from '@/lib/types'
import { cn } from '@/lib/utils'
import { fmtBytes } from '@/lib/format'
import { Markdown } from '@/components/markdown'
import { JsonBlock } from '@/components/json-block'
import { Button } from '@/components/ui/button'
import { ErrorState, LoadingRows } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'

function baseName(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts[parts.length - 1] || path
}

/** Split leading YAML frontmatter (`--- … ---`) from the markdown body. */
function splitFrontmatter(content: string): { fm: [string, string][]; body: string } {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(content)
  if (!m) return { fm: [], body: content }
  const fm: [string, string][] = []
  for (const line of m[1].split(/\r?\n/)) {
    const i = line.indexOf(':')
    if (i > 0) fm.push([line.slice(0, i).trim(), line.slice(i + 1).trim()])
  }
  return { fm, body: content.slice(m[0].length) }
}

/** A markdown file: frontmatter as a clean metadata table, body as markdown. */
function MarkdownFile({ content }: { content: string }) {
  const { fm, body } = splitFrontmatter(content)
  return (
    <div className="space-y-4">
      {fm.length > 0 && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 rounded-lg border border-border bg-muted/30 p-3 text-xs">
          {fm.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="font-mono text-muted-foreground">{k}</dt>
              <dd className="break-all font-mono text-foreground">{v || '—'}</dd>
            </div>
          ))}
        </dl>
      )}
      <Markdown>{body}</Markdown>
    </div>
  )
}

function TreeLevel({
  agentId,
  root,
  path,
  depth,
  selected,
  onSelect,
}: {
  agentId: string
  root: string
  path: string
  depth: number
  selected: string | null
  onSelect: (path: string) => void
}) {
  const q = useQuery<FilesTreeResponse>({
    queryKey: ['agent', agentId, 'files', root, path],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/files/tree?root=${root}${path ? `&path=${encodeURIComponent(path)}` : ''}`,
        signal,
      ),
  })

  if (q.isLoading) return <div className="py-1 pl-2 text-xs text-muted-foreground">Loading…</div>
  if (q.isError || !q.data) return null
  if (q.data.entries.length === 0)
    return <div className="py-1 pl-2 text-xs text-muted-foreground">empty</div>

  return (
    <ul>
      {q.data.entries.map((entry) => (
        <TreeNode
          key={entry.path}
          agentId={agentId}
          root={root}
          entry={entry}
          depth={depth}
          selected={selected}
          onSelect={onSelect}
        />
      ))}
    </ul>
  )
}

function TreeNode({
  agentId,
  root,
  entry,
  depth,
  selected,
  onSelect,
}: {
  agentId: string
  root: string
  entry: FilesTreeEntry
  depth: number
  selected: string | null
  onSelect: (path: string) => void
}) {
  const [open, setOpen] = useState(false)
  const isDir = entry.type === 'dir' || entry.type === 'directory'
  const name = baseName(entry.path)
  const pad = { paddingLeft: `${depth * 12 + 8}px` }

  if (isDir) {
    return (
      <li>
        <button
          type="button"
          style={pad}
          onClick={() => setOpen((o) => !o)}
          className="flex w-full cursor-pointer items-center gap-1.5 rounded-md py-1 pr-2 text-left text-sm font-medium text-foreground transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        >
          <ChevronRight className={cn('size-3.5 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')} />
          {open ? (
            <FolderOpen className="size-4 shrink-0 text-primary" />
          ) : (
            <Folder className="size-4 shrink-0 text-muted-foreground" />
          )}
          <span className="truncate">{name}</span>
        </button>
        {open && (
          <TreeLevel
            agentId={agentId}
            root={root}
            path={entry.path}
            depth={depth + 1}
            selected={selected}
            onSelect={onSelect}
          />
        )}
      </li>
    )
  }

  return (
    <li>
      <button
        type="button"
        style={pad}
        onClick={() => onSelect(entry.path)}
        className={cn(
          'flex w-full cursor-pointer items-center gap-1.5 rounded-md py-1 pr-2 text-left font-mono text-[13px] transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60',
          selected === entry.path
            ? 'bg-primary/10 text-foreground'
            : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <span className="w-3.5 shrink-0" />
        <File className={cn('size-4 shrink-0', selected === entry.path && 'text-primary')} />
        <span className="truncate">{name}</span>
      </button>
    </li>
  )
}

/** Read + (operator-only) edit for one workspace file. Markdown renders in
 *  view mode; Edit mode is a raw textarea saved via `PUT .../files/read`
 *  (COMP-012). Errors (400 path-escape/secret-content, 403 viewer) and a
 *  stale-signature warning surface verbatim from the server. */
function FileViewer({ agentId, root, path }: { agentId: string; root: string; path: string }) {
  const queryKey = ['agent', agentId, 'file', root, path]
  const q = useQuery<FileReadResponse>({
    queryKey,
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/files/read?root=${root}&path=${encodeURIComponent(path)}`, signal),
  })
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saveResult, setSaveResult] = useState<FileWriteResponse | null>(null)

  if (q.isLoading) return <LoadingRows rows={8} />
  if (q.isError) return <ErrorState error={q.error} />
  if (!q.data) return null

  const isMarkdown = path.endsWith('.md') || path.endsWith('.mdx')

  const startEdit = () => {
    setDraft(q.data!.content)
    setSaveError(null)
    setSaveResult(null)
    setEditing(true)
  }

  const save = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      const res = await apiPut<FileWriteResponse>(
        `/api/agents/${agentId}/files/read?root=${root}&path=${encodeURIComponent(path)}`,
        { content: draft },
      )
      setSaveResult(res)
      setEditing(false)
      await queryClient.invalidateQueries({ queryKey })
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2 text-xs text-muted-foreground">
        <span className="truncate font-mono">{path}</span>
        <div className="flex shrink-0 items-center gap-2">
          <span>{fmtBytes(q.data.size)}</span>
          {operatorMode && !editing && (
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
        <div className="border-b border-destructive/30 bg-destructive/10 px-4 py-2 text-xs text-destructive">
          {saveError}
        </div>
      )}
      {saveResult?.signature_stale && (
        <div className="border-b border-status-warning/30 bg-status-warning/10 px-4 py-2 text-xs text-status-warning">
          {saveResult.message}
        </div>
      )}
      {saveResult && !saveResult.signature_stale && (
        <div className="border-b border-status-online/30 bg-status-online/10 px-4 py-2 text-xs text-status-online">
          {saveResult.message}
        </div>
      )}
      <div className="flex-1 overflow-auto p-4">
        {editing ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="h-full min-h-[240px] w-full resize-none rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          />
        ) : isMarkdown ? (
          <MarkdownFile content={q.data.content} />
        ) : (
          <JsonBlock value={q.data.content} className="border-0 bg-transparent p-0" />
        )}
      </div>
    </div>
  )
}

/** Two-pane file browser: lazy tree (left) + file viewer (right).
 *  `root` selects the agent's `workspace` subdir or full `agent` root. */
export function FileTree({
  agentId,
  root = 'workspace',
  rootLabel,
}: {
  agentId: string
  root?: 'workspace' | 'agent'
  rootLabel?: string
}) {
  const [selected, setSelected] = useState<string | null>(null)
  // Reset selection when switching roots so the viewer never shows a stale file.
  return (
    <div className="grid h-[460px] grid-cols-[minmax(220px,300px)_1fr] overflow-hidden rounded-lg border border-border bg-card shadow-xs">
      <div className="overflow-auto border-r border-border p-2">
        <div className="px-2 pb-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {rootLabel ?? root}
        </div>
        <TreeLevel agentId={agentId} root={root} path="" depth={0} selected={selected} onSelect={setSelected} />
      </div>
      <div className="overflow-hidden">
        {selected ? (
          <FileViewer agentId={agentId} root={root} path={selected} />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Select a file to view
          </div>
        )}
      </div>
    </div>
  )
}
