export const MIN_PASSWORD_LENGTH = 12

export interface SessionGrant {
  token: string
}

/** POST JSON without a bearer: these routes run before anyone is signed in. */
export async function postPublic<T>(
  path: string,
  body: unknown,
): Promise<{ ok: true; data: T } | { ok: false; error: string }> {
  try {
    const resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) return { ok: false, error: data.error || 'Something went wrong.' }
    return { ok: true, data: data as T }
  } catch {
    return { ok: false, error: 'Could not reach the server.' }
  }
}

/** Returns a plain sentence when the two password fields are not acceptable. */
export function passwordProblem(password: string, repeat: string): string {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Use at least ${MIN_PASSWORD_LENGTH} characters for the password.`
  }
  if (password !== repeat) return 'The two passwords do not match.'
  return ''
}
