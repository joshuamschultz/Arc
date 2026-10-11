// SPEC-088 COMP-016: turn a waiting step's JSON Schema into a payload.
//
// A flat object schema (every property a string, number, integer, boolean or
// string enum) becomes a typed form; anything else (nested objects, arrays,
// combinators, non-string enums) is edited as raw JSON. Either way the server
// re-validates the payload against the same schema and answers 422 per field:
// these checks only save a round trip, they decide nothing.

export type FormFieldType = 'string' | 'number' | 'integer' | 'boolean' | 'enum'

export interface FormField {
  name: string
  label: string
  type: FormFieldType
  required: boolean
  description?: string
  options?: string[]
}

/** What the person has typed so far: form values, and the raw JSON text. */
export interface PayloadDraft {
  values: Record<string, string | boolean>
  json: string
}

/** A fresh draft: no form values, an empty JSON object. */
export const EMPTY_DRAFT: PayloadDraft = { values: {}, json: '{}' }

/** The key a raw-JSON problem is reported under (no property is named this). */
export const JSON_PROBLEM = '$json'

export type PayloadResult =
  | { payload: Record<string, unknown> }
  | { problems: Record<string, string> }

const SCALAR_TYPES = new Set(['string', 'number', 'integer', 'boolean'])

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

function fieldOf(name: string, prop: unknown, required: Set<string>): FormField | null {
  if (!isRecord(prop)) return null
  const base = {
    name,
    label: typeof prop.title === 'string' && prop.title ? prop.title : name,
    required: required.has(name),
    description: typeof prop.description === 'string' ? prop.description : undefined,
  }
  if (Array.isArray(prop.enum)) {
    const options = prop.enum
    return options.length > 0 && options.every((o) => typeof o === 'string')
      ? { ...base, type: 'enum', options: options as string[] }
      : null
  }
  return typeof prop.type === 'string' && SCALAR_TYPES.has(prop.type)
    ? { ...base, type: prop.type as FormFieldType }
    : null
}

/** One field per property of a flat object schema; `null` when the form cannot carry it. */
export function flatFormFields(schema: unknown): FormField[] | null {
  if (!isRecord(schema) || schema.type !== 'object' || !isRecord(schema.properties)) return null
  const required = new Set(
    Array.isArray(schema.required) ? schema.required.filter((r) => typeof r === 'string') : [],
  )
  const fields: FormField[] = []
  for (const [name, prop] of Object.entries(schema.properties)) {
    const field = fieldOf(name, prop, required)
    if (field === null) return null
    fields.push(field)
  }
  return fields.length > 0 ? fields : null
}

function readField(field: FormField, raw: string | boolean | undefined): { value?: unknown; problem?: string } {
  if (field.type === 'boolean') return { value: raw === true }
  const text = typeof raw === 'string' ? raw.trim() : ''
  if (!text) return field.required ? { problem: `${field.label} is required` } : {}
  if (field.type === 'string' || field.type === 'enum') return { value: text }
  const n = Number(text)
  if (!Number.isFinite(n)) return { problem: `${field.label} must be a number` }
  if (field.type === 'integer' && !Number.isInteger(n)) {
    return { problem: `${field.label} must be a whole number` }
  }
  return { value: n }
}

/** The form's values as a typed payload; empty optional fields are left out. */
export function formPayload(fields: FormField[], values: PayloadDraft['values']): PayloadResult {
  const payload: Record<string, unknown> = {}
  const problems: Record<string, string> = {}
  for (const field of fields) {
    const { value, problem } = readField(field, values[field.name])
    if (problem) problems[field.name] = problem
    else if (value !== undefined) payload[field.name] = value
  }
  return Object.keys(problems).length ? { problems } : { payload }
}

/** Raw JSON text as an object payload; empty text is the empty object. */
export function jsonPayload(text: string): PayloadResult {
  if (!text.trim()) return { payload: {} }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return { problems: { [JSON_PROBLEM]: 'The payload is not valid JSON.' } }
  }
  return isRecord(parsed)
    ? { payload: parsed }
    : { problems: { [JSON_PROBLEM]: 'The payload must be a JSON object.' } }
}

/** The payload a draft makes under `schema`: the form when it fits, else the JSON. */
export function draftPayload(schema: unknown, draft: PayloadDraft): PayloadResult {
  const fields = flatFormFields(schema)
  return fields ? formPayload(fields, draft.values) : jsonPayload(draft.json)
}

/**
 * Server field issues keyed by the property they name. The server reports a
 * payload issue as `payload.<field>` (gate) or `input.<field>` (event); a
 * whole-payload issue lands under {@link JSON_PROBLEM}.
 */
export function problemsFromIssues(
  issues: ReadonlyArray<{ field?: unknown; error?: unknown }>,
): Record<string, string> {
  const problems: Record<string, string> = {}
  for (const issue of issues) {
    const field = String(issue.field ?? '')
    const dot = field.indexOf('.')
    const key = dot >= 0 ? field.slice(dot + 1) : JSON_PROBLEM
    problems[key] = String(issue.error ?? 'invalid')
  }
  return problems
}
