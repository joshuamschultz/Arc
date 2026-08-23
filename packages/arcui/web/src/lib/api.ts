import { getToken } from './auth'

/** Thrown on any non-2xx API response; carries the server's `{error}` text.
 *
 * `errors` is populated when the body is a typed field-error list (SPEC-061
 * ArcFlow's `{"errors": [{node_id, field, error, observed, admissible}]}`,
 * COMP-002) — callers that need to render against the offending node/field
 * (rather than a single flattened message) read this instead of `.message`.
 */
export class ApiError extends Error {
  status: number
  errors?: Array<Record<string, unknown>>
  /** The decoded error body when it was JSON. Routes that attach extra keys a
   * surface needs to act on (SPEC-064's `unsatisfied_host`) are read from here. */
  body?: Record<string, unknown>
  constructor(
    status: number,
    message: string,
    errors?: Array<Record<string, unknown>>,
    body?: Record<string, unknown>,
  ) {
    super(message)
    this.status = status
    this.errors = errors
    this.body = body
    this.name = 'ApiError'
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function parseError(
  res: Response,
): Promise<{
  message: string
  errors?: Array<Record<string, unknown>>
  body?: Record<string, unknown>
}> {
  try {
    // Most routes use `ErrorResponse{error}`; the knowledge mutation routes
    // (COMP-002) return `{status, results: [{error}]}` on a 404/500 instead;
    // workflow routes (SPEC-061) return `{errors: [{node_id, field, ...}]}`
    // on a validation rejection — check all three so a failed request
    // surfaces its real reason verbatim.
    const body = (await res.json()) as {
      error?: string
      errors?: Array<Record<string, unknown>>
      results?: Array<{ error?: string | null }>
    }
    if (Array.isArray(body?.errors) && body.errors.length) {
      return {
        message: body.errors.map((e) => `${e.field}: ${e.error}`).join('; '),
        errors: body.errors,
        body,
      }
    }
    if (body?.error) return { message: body.error, body }
    const resultErrors = body?.results?.map((r) => r.error).filter(Boolean)
    if (resultErrors?.length) return { message: resultErrors.join('; '), body }
  } catch {
    /* not JSON */
  }
  return { message: `HTTP ${res.status}` }
}

/** GET `path`, returning parsed JSON. Throws `ApiError` on failure. */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { headers: authHeaders(), signal })
  if (!res.ok) {
    const parsed = await parseError(res)
    throw new ApiError(res.status, parsed.message, parsed.errors, parsed.body)
  }
  return (await res.json()) as T
}

async function apiSend<T>(
  method: 'POST' | 'PATCH' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { ...authHeaders(), 'Content-Type': 'application/json', ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    const parsed = await parseError(res)
    throw new ApiError(res.status, parsed.message, parsed.errors, parsed.body)
  }
  // 204 No Content (e.g. DELETE) carries no body — parsing it as JSON would throw.
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const apiPost = <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
  apiSend<T>('POST', path, body, headers)
export const apiPatch = <T>(path: string, body?: unknown) =>
  apiSend<T>('PATCH', path, body)
export const apiPut = <T>(path: string, body?: unknown) =>
  apiSend<T>('PUT', path, body)
export const apiDelete = <T>(path: string, body?: unknown) => apiSend<T>('DELETE', path, body)
