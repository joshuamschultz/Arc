import { useRef, useState, type DragEvent } from 'react'
import { CheckCircle2, FilePlus2, LoaderCircle, RotateCcw, Trash2, X, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { AttachmentItem, AttachmentStatus } from '@/hooks/use-attachment-uploader'

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function statusLabel(status: AttachmentStatus): string {
  if (status === 'uploading') return 'Uploading'
  if (status === 'clean') return 'Ready'
  if (status === 'rejected') return 'Rejected'
  if (status === 'failed') return 'Upload failed'
  if (status === 'canceled') return 'Canceled'
  return 'Waiting'
}

export function AttachmentPicker({
  items,
  onAdd,
  onCancel,
  onRetry,
  onRemove,
  disabled = false,
}: {
  items: AttachmentItem[]
  onAdd: (files: FileList | File[]) => void
  onCancel: (id: string) => void
  onRetry: (id: string) => void
  onRemove: (id: string) => void
  disabled?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  const acceptFiles = (files: FileList | File[]) => {
    if (!disabled && files.length > 0) onAdd(files)
  }
  const drop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    acceptFiles(event.dataTransfer.files)
  }

  return (
    <div className="flex flex-col gap-2" aria-label="Message attachments">
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (!disabled && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault()
            inputRef.current?.click()
          }
        }}
        onDragEnter={(event) => {
          event.preventDefault()
          if (!disabled) setDragging(true)
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
        className={cn(
          'flex min-h-10 cursor-pointer items-center justify-center gap-2 rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground transition-colors',
          dragging ? 'border-primary bg-primary/10 text-primary' : 'border-border hover:border-primary/50 hover:text-foreground',
          disabled && 'cursor-not-allowed opacity-50',
        )}
      >
        <FilePlus2 className="size-4" aria-hidden="true" />
        <span>Drop files here or choose attachments</span>
        <input
          ref={inputRef}
          className="sr-only"
          type="file"
          multiple
          accept="*/*"
          disabled={disabled}
          onChange={(event) => {
            if (event.currentTarget.files) acceptFiles(event.currentTarget.files)
            event.currentTarget.value = ''
          }}
          aria-label="Choose attachments"
        />
      </div>
      {items.length > 0 && (
        <ul className="flex flex-col gap-1.5" aria-label="Selected attachments">
          {items.map((item) => (
            <li key={item.localId} className="flex items-center gap-2 rounded-md border border-border bg-background/60 px-2.5 py-2 text-xs">
              {item.status === 'clean' ? (
                <CheckCircle2 className="size-4 shrink-0 text-primary" aria-hidden="true" />
              ) : item.status === 'rejected' || item.status === 'failed' || item.status === 'canceled' ? (
                <XCircle className="size-4 shrink-0 text-destructive" aria-hidden="true" />
              ) : (
                <LoaderCircle className="size-4 shrink-0 animate-spin text-muted-foreground" aria-hidden="true" />
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-foreground" title={item.name}>{item.name}</span>
                <span className="flex gap-2 text-[10px] text-muted-foreground">
                  <span>{formatSize(item.size)}</span>
                  <span>{statusLabel(item.status)}</span>
                  {item.status === 'uploading' && <span>{item.progress}%</span>}
                </span>
                {item.error && <span className="block truncate text-[10px] text-destructive">{item.error}</span>}
              </span>
              {item.status === 'uploading' ? (
                <Button type="button" variant="ghost" size="icon-xs" onClick={() => onCancel(item.localId)} aria-label={`Cancel ${item.name}`} title="Cancel upload">
                  <X className="size-3.5" />
                </Button>
              ) : item.status === 'failed' || item.status === 'canceled' ? (
                <Button type="button" variant="ghost" size="icon-xs" onClick={() => onRetry(item.localId)} aria-label={`Retry ${item.name}`} title="Retry upload">
                  <RotateCcw className="size-3.5" />
                </Button>
              ) : null}
              <Button type="button" variant="ghost" size="icon-xs" onClick={() => onRemove(item.localId)} aria-label={`Remove ${item.name}`} title="Remove attachment">
                <Trash2 className="size-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
