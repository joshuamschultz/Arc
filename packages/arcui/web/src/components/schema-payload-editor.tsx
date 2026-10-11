import { useId } from 'react'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { JSON_PROBLEM, flatFormFields, type FormField, type PayloadDraft } from '@/lib/schema-form'

/** Native select styled like `Input`: a short closed list reads best as one. */
const SELECT_CLASS =
  'h-9 w-full rounded-md border border-input bg-transparent px-3 text-base shadow-xs outline-none hover:border-muted-foreground/40 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/60 aria-invalid:border-destructive md:text-sm dark:bg-input/30'

function FieldControl({
  field,
  id,
  value,
  invalid,
  describedBy,
  onChange,
}: {
  field: FormField
  id: string
  value: string | boolean | undefined
  invalid: boolean
  describedBy?: string
  onChange: (value: string | boolean) => void
}) {
  const common = { id, 'aria-invalid': invalid || undefined, 'aria-describedby': describedBy }
  if (field.type === 'enum') {
    return (
      <select
        {...common}
        className={SELECT_CLASS}
        value={typeof value === 'string' ? value : ''}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Choose…</option>
        {field.options?.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    )
  }
  return (
    <Input
      {...common}
      inputMode={field.type === 'string' ? undefined : field.type === 'integer' ? 'numeric' : 'decimal'}
      value={typeof value === 'string' ? value : ''}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

function FormRow({
  field,
  value,
  problem,
  onChange,
}: {
  field: FormField
  value: string | boolean | undefined
  problem?: string
  onChange: (value: string | boolean) => void
}) {
  const id = useId()
  const noteId = `${id}-note`
  const note = problem ?? field.description
  if (field.type === 'boolean') {
    return (
      <div className="space-y-0.5">
        <label htmlFor={id} className="flex items-center gap-2 text-sm text-foreground">
          <input
            id={id}
            type="checkbox"
            aria-describedby={note ? noteId : undefined}
            checked={value === true}
            onChange={(e) => onChange(e.target.checked)}
          />
          {field.label}
        </label>
        {note && (
          <p id={noteId} className={`text-xs ${problem ? 'text-destructive' : 'text-muted-foreground'}`}>
            {note}
          </p>
        )}
      </div>
    )
  }
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-xs font-medium text-foreground">
        {field.label}
        {field.required && <span className="text-muted-foreground"> (required)</span>}
      </label>
      <FieldControl
        field={field}
        id={id}
        value={value}
        invalid={Boolean(problem)}
        describedBy={note ? noteId : undefined}
        onChange={onChange}
      />
      {note && (
        <p id={noteId} className={`text-xs ${problem ? 'text-destructive' : 'text-muted-foreground'}`}>
          {note}
        </p>
      )}
    </div>
  )
}

/**
 * Edits the payload a waiting step's schema asks for (SPEC-088 COMP-016).
 *
 * A flat object schema renders one labelled control per property; any other
 * schema is edited as raw JSON. The draft lives with the caller, which turns
 * it into a payload with `draftPayload` when the person decides.
 */
export function SchemaPayloadEditor({
  schema,
  draft,
  problems,
  onDraft,
}: {
  schema: unknown
  draft: PayloadDraft
  problems: Record<string, string>
  onDraft: (draft: PayloadDraft) => void
}) {
  const jsonId = useId()
  const fields = flatFormFields(schema)
  if (fields) {
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        {fields.map((field) => (
          <FormRow
            key={field.name}
            field={field}
            value={draft.values[field.name]}
            problem={problems[field.name]}
            onChange={(value) => onDraft({ ...draft, values: { ...draft.values, [field.name]: value } })}
          />
        ))}
      </div>
    )
  }
  const problem = problems[JSON_PROBLEM]
  return (
    <div className="space-y-1">
      <label htmlFor={jsonId} className="block text-xs font-medium text-foreground">
        Payload (JSON)
      </label>
      <Textarea
        id={jsonId}
        rows={4}
        spellCheck={false}
        aria-invalid={Boolean(problem) || undefined}
        className="min-h-24 font-mono text-xs"
        value={draft.json}
        onChange={(e) => onDraft({ ...draft, json: e.target.value })}
      />
      {Object.entries(problems).map(([key, text]) => (
        <p key={key} className="text-xs text-destructive">
          {key === JSON_PROBLEM ? text : `${key}: ${text}`}
        </p>
      ))}
    </div>
  )
}
