// Viewer/operator token handling. Mirrors the old index.html bootstrap:
// the token arrives in the URL fragment as `#auth=<token>`, is persisted to
// localStorage, and the fragment is stripped before anything else runs so it
// can't leak via Referer. The same token is sent as the REST bearer header
// and as the first WS frame; the server replies with the role it grants.

const TOKEN_KEY = 'arcui_viewer_token'

/** Consume `#auth=<token>` from the URL and persist it. Call once, first. */
export function bootstrapAuth(): void {
  const hash = window.location.hash || ''
  const match = hash.match(/[#&]auth=([^&]+)/)
  if (!match) return
  try {
    localStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]))
  } catch {
    /* localStorage disabled — fall through to manual entry */
  }
  history.replaceState(null, '', window.location.pathname + window.location.search)
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token.trim())
  } catch {
    /* ignore */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

export function hasToken(): boolean {
  return getToken().length > 0
}
