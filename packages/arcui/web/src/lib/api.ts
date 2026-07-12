import { getToken } from './auth'

/** Thrown on any non-2xx API response; carries the server's `{error}` text. */
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function parseError(res: Response): Promise<string> {
  try {
    // Most routes use `ErrorResponse{error}`; the knowledge mutation routes
    // (COMP-002) return `{status, results: [{error}]}` on a 404/500 instead —
    // check both so a failed edit/delete surfaces its real reason verbatim.
    const body = (await res.json()) as {
      error?: string
      results?: Array<{ error?: string | null }>
    }
    if (body?.error) return body.error
    const resultErrors = body?.results?.map((r) => r.error).filter(Boolean)
    if (resultErrors?.length) return resultErrors.join('; ')
  } catch {
    /* not JSON */
  }
  return `HTTP ${res.status}`
}

/** GET `path`, returning parsed JSON. Throws `ApiError` on failure. */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { headers: authHeaders(), signal })
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return (await res.json()) as T
}

async function apiSend<T>(
  method: 'POST' | 'PATCH' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  // 204 No Content (e.g. DELETE) carries no body — parsing it as JSON would throw.
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const apiPost = <T>(path: string, body?: unknown) =>
  apiSend<T>('POST', path, body)
export const apiPatch = <T>(path: string, body?: unknown) =>
  apiSend<T>('PATCH', path, body)
export const apiPut = <T>(path: string, body?: unknown) =>
  apiSend<T>('PUT', path, body)
export const apiDelete = <T>(path: string, body?: unknown) => apiSend<T>('DELETE', path, body)
