import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, File, Folder, FolderOpen } from 'lucide-react'
import { apiGet } from '@/lib/api'
import type { FileReadResponse, FilesTreeEntry, FilesTreeResponse } from '@/lib/types'
import { cn } from '@/lib/utils'
import { fmtBytes } from '@/lib/format'
import { Markdown } from '@/components/markdown'
import { JsonBlock } from '@/components/json-block'
import { ErrorState, LoadingRows } from '@/components/states'

function baseName(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts[parts.length - 1] || path
}

function TreeLevel({
  agentId,
  path,
  depth,
  selected,
  onSelect,
}: {
  agentId: string
  path: string
  depth: number
  selected: string | null
  onSelect: (path: string) => void
}) {
  const q = useQuery<FilesTreeResponse>({
    queryKey: ['agent', agentId, 'files', path],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/files/tree${path ? `?path=${encodeURIComponent(path)}` : ''}`,
        signal,
      ),
  })

  if (q.isLoading) return <div className="py-1 pl-2 text-xs text-muted-foreground">Loading…</div>
  if (q.isError || !q.data) return null

  return (
    <ul>
      {q.data.entries.map((entry) => (
        <TreeNode
          key={entry.path}
          agentId={agentId}
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
  entry,
  depth,
  selected,
  onSelect,
}: {
  agentId: string
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
          className="flex w-full items-center gap-1.5 rounded py-1 pr-2 text-left text-sm text-foreground hover:bg-muted/50"
        >
          <ChevronRight className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-90')} />
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
          'flex w-full items-center gap-1.5 rounded py-1 pr-2 text-left text-sm hover:bg-muted/50',
          selected === entry.path ? 'bg-primary/10 text-foreground' : 'text-muted-foreground',
        )}
      >
        <span className="w-3.5 shrink-0" />
        <File className="size-4 shrink-0" />
        <span className="truncate">{name}</span>
      </button>
    </li>
  )
}

function FileViewer({ agentId, path }: { agentId: string; path: string }) {
  const q = useQuery<FileReadResponse>({
    queryKey: ['agent', agentId, 'file', path],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/files/read?path=${encodeURIComponent(path)}`, signal),
  })

  if (q.isLoading) return <LoadingRows rows={8} />
  if (q.isError) return <ErrorState error={q.error} />
  if (!q.data) return null

  const isMarkdown = path.endsWith('.md') || path.endsWith('.mdx')
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-2 text-xs text-muted-foreground">
        <span className="truncate font-mono">{path}</span>
        <span className="shrink-0">{fmtBytes(q.data.size)}</span>
      </div>
      <div className="flex-1 overflow-auto p-4">
        {isMarkdown ? (
          <Markdown>{q.data.content}</Markdown>
        ) : (
          <JsonBlock value={q.data.content} className="border-0 bg-transparent p-0" />
        )}
      </div>
    </div>
  )
}

/** Two-pane workspace browser: lazy tree (left) + file viewer (right). */
export function FileTree({ agentId, rootLabel = 'workspace' }: { agentId: string; rootLabel?: string }) {
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <div className="grid h-[460px] grid-cols-[minmax(220px,300px)_1fr] overflow-hidden rounded-xl border border-border bg-card">
      <div className="overflow-auto border-r border-border p-2">
        <div className="px-2 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {rootLabel}
        </div>
        <TreeLevel agentId={agentId} path="" depth={0} selected={selected} onSelect={setSelected} />
      </div>
      <div className="overflow-hidden">
        {selected ? (
          <FileViewer agentId={agentId} path={selected} />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Select a file to view
          </div>
        )}
      </div>
    </div>
  )
}
