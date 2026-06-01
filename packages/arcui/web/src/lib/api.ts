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
    const body = (await res.json()) as { error?: string }
    if (body?.error) return body.error
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
  method: 'POST' | 'PATCH' | 'PUT',
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, await parseError(res))
  return (await res.json()) as T
}

export const apiPost = <T>(path: string, body?: unknown) =>
  apiSend<T>('POST', path, body)
export const apiPatch = <T>(path: string, body?: unknown) =>
  apiSend<T>('PATCH', path, body)
