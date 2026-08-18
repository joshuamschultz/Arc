import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil, Check, X, Lock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { apiPatch, ApiError } from '@/lib/api'
import { ReadValue } from '@/components/settings-view/config-value'

// One top-level TOML table, rendered as a flat card. Read-only shows the
// key/value tree; operator mode swaps in a JSON editor that PATCHes just this
// section. A failed parse or save surfaces inline — the edit never silently
// drops.
export function ConfigSection({
  endpoint,
  queryKey,
  sectionKey,
  value,
  editable,
}: {
  endpoint: string
  queryKey: unknown[]
  sectionKey: string
  value: unknown
  editable: boolean
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const startEdit = () => {
    setDraft(JSON.stringify(value, null, 2))
    setError(null)
    setEditing(true)
  }

  const save = async () => {
    let parsed: unknown
    try {
      parsed = JSON.parse(draft)
    } catch {
      setError('That is not valid JSON — check for a missing comma or quote.')
      return
    }
    setSaving(true)
    try {
      await apiPatch(endpoint, { [sectionKey]: parsed })
      await queryClient.invalidateQueries({ queryKey })
      setEditing(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card transition-colors duration-150 hover:border-foreground/15">
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <h3 className="truncate rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs font-semibold text-foreground">
          {sectionKey}
        </h3>
        {editable ? (
          editing ? (
            <div className="flex shrink-0 gap-1">
              <Button variant="ghost" size="sm" onClick={() => setEditing(false)} disabled={saving}>
                <X className="size-3.5" /> Cancel
              </Button>
              <Button size="sm" onClick={save} disabled={saving}>
                <Check className="size-3.5" /> {saving ? 'Saving…' : 'Save'}
              </Button>
            </div>
          ) : (
            <Button variant="ghost" size="sm" onClick={startEdit}>
              <Pencil className="size-3.5" /> Edit
            </Button>
          )
        ) : (
          <span
            className="grid size-6 shrink-0 place-items-center text-muted-foreground/60"
            title="Read-only — turn on operator controls to edit"
          >
            <Lock className="size-3.5" />
          </span>
        )}
      </div>
      {editing ? (
        <div className="space-y-2 p-4">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            className="h-48 w-full rounded-md border border-border bg-muted/30 p-2.5 font-mono text-xs leading-relaxed text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          />
          {error ? (
            <p className="text-xs text-destructive">{error}</p>
          ) : (
            <p className="text-[11px] text-muted-foreground">
              Edit the values as JSON. The server validates and re-signs this file when you save.
            </p>
          )}
        </div>
      ) : (
        <div className="p-4">
          <ReadValue value={value} />
        </div>
      )}
    </div>
  )
}
