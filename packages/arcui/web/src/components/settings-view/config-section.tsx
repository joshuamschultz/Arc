import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil, Check, X, Lock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { apiPatch, ApiError } from '@/lib/api'
import { FieldHelp } from '@/components/help'
import { configHelpKey } from '@/lib/help'
import { ReadValue } from '@/components/settings-view/config-value'

type ConfigObject = Record<string, unknown>

function isObject(value: unknown): value is ConfigObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function updatePath(root: ConfigObject, path: string[], value: unknown): ConfigObject {
  const next = structuredClone(root)
  let cursor = next
  for (const segment of path.slice(0, -1)) {
    cursor = cursor[segment] as ConfigObject
  }
  cursor[path[path.length - 1]] = value
  return next
}

function invalidNumber(draft: ConfigObject, template: ConfigObject): boolean {
  return Object.entries(template).some(([key, original]) => {
    const current = draft[key]
    if (typeof original === 'number') return typeof current !== 'number' || !Number.isFinite(current)
    return isObject(original) && isObject(current) && invalidNumber(current, original)
  })
}

function GuidedFields({
  value,
  template,
  file,
  path,
  onChange,
}: {
  value: ConfigObject
  template: ConfigObject
  file: string
  path: string[]
  onChange: (path: string[], value: unknown) => void
}) {
  return (
    <div className="space-y-3">
      {Object.entries(value).map(([key, current]) => {
        const fieldPath = [...path, key]
        const original = template[key]
        if (isObject(current)) {
          return (
            <fieldset key={key} className="space-y-2 rounded-md border border-border p-3">
              <legend className="px-1 font-mono text-xs font-semibold">{key}</legend>
              <GuidedFields value={current} template={original as ConfigObject} file={file} path={fieldPath} onChange={onChange} />
            </fieldset>
          )
        }
        if (typeof current !== 'string' && typeof current !== 'number' && typeof current !== 'boolean') {
          return (
            <p key={key} className="text-xs text-muted-foreground">
              {key}: this value can be changed in Advanced JSON.
              <FieldHelp helpKey={configHelpKey(file, fieldPath)} route="settings" />
            </p>
          )
        }
        const helpKey = configHelpKey(file, fieldPath)
        const inputId = `${file}-${fieldPath.join('-')}`
        return (
          <div key={key} className="space-y-1">
            <div className="flex items-center gap-1">
              <label htmlFor={inputId} className="font-mono text-xs font-medium">{key}</label>
              <FieldHelp helpKey={helpKey} route="settings" />
            </div>
            {typeof current === 'boolean' ? (
              <input
                id={inputId}
                type="checkbox"
                checked={current}
                onChange={(event) => onChange(fieldPath, event.target.checked)}
                className="size-4 accent-primary"
              />
            ) : (
              <input
                id={inputId}
                type={typeof original === 'number' ? 'number' : 'text'}
                step={typeof original === 'number' ? 'any' : undefined}
                value={current}
                onChange={(event) => onChange(
                  fieldPath,
                  typeof original === 'number' && event.target.value !== ''
                    ? Number(event.target.value)
                    : event.target.value,
                )}
                className="w-full rounded-md border border-border bg-background px-2.5 py-1.5 font-mono text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            )}
          </div>
        )
      })}
    </div>
  )
}

/** One top-level TOML section, with guided scalar fields and an advanced JSON editor. */
export function ConfigSection({
  endpoint,
  queryKey,
  sectionKey,
  file,
  value,
  editable,
}: {
  endpoint: string
  queryKey: unknown[]
  sectionKey: string
  file: string
  value: unknown
  editable: boolean
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [draft, setDraft] = useState<unknown>(null)
  const [rawDraft, setRawDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const startEdit = () => {
    setDraft(structuredClone(value))
    setRawDraft(JSON.stringify(value, null, 2))
    setAdvanced(!isObject(value))
    setError(null)
    setEditing(true)
  }

  const save = async () => {
    let parsed = draft
    if (advanced) {
      try {
        parsed = JSON.parse(rawDraft) as unknown
      } catch {
        setError('That is not valid JSON — check for a missing comma or quote.')
        return
      }
    }
    if (!advanced && isObject(parsed) && isObject(value) && invalidNumber(parsed, value)) {
      setError('Enter a valid number for each numeric setting.')
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
          <FieldHelp helpKey={isObject(value) ? `settings.section.${file}` : configHelpKey(file, [sectionKey])} route="settings" />
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
          <span className="grid size-6 shrink-0 place-items-center text-muted-foreground/60" title="Read-only — turn on operator controls to edit">
            <Lock className="size-3.5" />
          </span>
        )}
      </div>
      {editing ? (
        <div className="space-y-3 p-4">
          <button
            type="button"
            className="text-xs text-primary underline-offset-2 hover:underline"
            onClick={() => {
              if (!advanced) {
                setRawDraft(JSON.stringify(draft, null, 2))
              } else {
                try {
                  setDraft(JSON.parse(rawDraft) as unknown)
                } catch {
                  setError('Fix the JSON before returning to guided fields.')
                  return
                }
              }
              setAdvanced(!advanced)
              setError(null)
            }}
          >
            {advanced ? 'Guided fields' : 'Advanced JSON'}
          </button>
          {advanced ? (
            <>
              <label htmlFor={`${file}-${sectionKey}-json`} className="block text-xs font-medium">{sectionKey} JSON</label>
              <FieldHelp helpKey={isObject(value) ? 'settings.config_value' : configHelpKey(file, [sectionKey])} route="settings" />
              <textarea
                id={`${file}-${sectionKey}-json`}
                value={rawDraft}
                onChange={(event) => setRawDraft(event.target.value)}
                spellCheck={false}
                className="h-48 w-full rounded-md border border-border bg-muted/30 p-2.5 font-mono text-xs leading-relaxed text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              />
            </>
          ) : isObject(draft) ? (
            <GuidedFields
              value={draft}
              template={value as ConfigObject}
              file={file}
              path={[sectionKey]}
              onChange={(path, next) => setDraft((current: unknown) => updatePath(current as ConfigObject, path.slice(1), next))}
            />
          ) : null}
          {error ? <p role="alert" className="text-xs text-destructive">{error}</p> : (
            <p className="text-[11px] text-muted-foreground">The server validates and re-signs this file when you save.</p>
          )}
        </div>
      ) : (
        <div className="p-4"><ReadValue value={value} file={file} path={[sectionKey]} /></div>
      )}
    </div>
  )
}
