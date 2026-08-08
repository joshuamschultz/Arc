import { useEffect, useState, type ReactNode } from 'react'
import { ArcLogo } from '@/components/arc-logo'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { hasToken, setToken } from '@/lib/auth'

/**
 * Blocks the app until the caller is authenticated.
 *
 * Two ways in, and the order is deliberate. Signing in with an email and
 * password produces a session that carries the person's DID, so what they
 * approve is recorded against them. Pasting a viewer/operator token still works
 * — it is how automation connects, how a fresh install connects before any
 * account exists, and how the owner gets in if the account store is unreadable
 * — but it names nobody, so it is the fallback rather than the front door.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState(hasToken())
  const [loginAvailable, setLoginAvailable] = useState<boolean | null>(null)
  const [useToken, setUseToken] = useState(false)

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [tokenValue, setTokenValue] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (authed) return
    let cancelled = false
    fetch('/api/auth/mode')
      .then((r) => r.json())
      .then((d) => !cancelled && setLoginAvailable(Boolean(d.login_available)))
      // If we cannot tell, offer the token box: it always works.
      .catch(() => !cancelled && setLoginAvailable(false))
    return () => {
      cancelled = true
    }
  }, [authed])

  if (authed) return <>{children}</>

  const submitLogin = async () => {
    if (!email.trim() || !password) return
    setBusy(true)
    setError('')
    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), password }),
      })
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok) {
        setError(data.error || 'Could not sign in.')
        return
      }
      setToken(data.token)
      setAuthed(true)
    } catch {
      setError('Could not reach the server.')
    } finally {
      setBusy(false)
    }
  }

  const submitToken = () => {
    const v = tokenValue.trim()
    if (!v) return
    setToken(v)
    setAuthed(true)
  }

  const showLogin = loginAvailable === true && !useToken

  return (
    <div className="flex h-screen items-center justify-center bg-background p-6">
      <div className="w-full max-w-sm rounded-xl border border-border bg-card p-8 shadow-lg">
        <div className="mb-5 flex items-center gap-2">
          <ArcLogo />
          <span className="text-lg font-bold tracking-wide text-foreground">ARC</span>
        </div>

        {showLogin ? (
          <>
            <h1 className="text-base font-semibold text-foreground">Sign in</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Use the account for this deployment.
            </p>
            <div className="mt-5 flex flex-col gap-2">
              <Input
                type="email"
                autoFocus
                autoComplete="username"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && submitLogin()}
              />
              <Input
                type="password"
                autoComplete="current-password"
                placeholder="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && submitLogin()}
              />
              <Button onClick={submitLogin} disabled={busy || !email.trim() || !password}>
                {busy ? 'Signing in…' : 'Sign in'}
              </Button>
            </div>
          </>
        ) : (
          <>
            <h1 className="text-base font-semibold text-foreground">Authentication required</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {loginAvailable === false ? (
                <>
                  No accounts exist yet. Create one with
                  <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">
                    arc user add you@example.com
                  </code>
                  , or paste a viewer or operator token.
                </>
              ) : (
                <>
                  Paste a viewer or operator token. The CLI prints these on
                  <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">arc ui start</code>.
                </>
              )}
            </p>
            <div className="mt-5 flex flex-col gap-2">
              <Input
                type="password"
                autoFocus
                placeholder="viewer token"
                value={tokenValue}
                onChange={(e) => setTokenValue(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && submitToken()}
              />
              <Button onClick={submitToken} disabled={!tokenValue.trim()}>
                Connect
              </Button>
            </div>
          </>
        )}

        {error && (
          <p role="alert" className="mt-3 text-sm text-destructive">
            {error}
          </p>
        )}

        {loginAvailable === true && (
          <button
            type="button"
            className="mt-4 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
            onClick={() => {
              setUseToken(!useToken)
              setError('')
            }}
          >
            {useToken ? 'Sign in with an account instead' : 'Use a token instead'}
          </button>
        )}
      </div>
    </div>
  )
}
