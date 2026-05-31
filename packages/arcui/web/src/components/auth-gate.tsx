import { useState, type ReactNode } from 'react'
import { ArcLogo } from '@/components/arc-logo'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { hasToken, setToken } from '@/lib/auth'
import { arcSocket } from '@/lib/arc-socket'
import { useConnectionStore } from '@/store/connection'

/**
 * Blocks the app until a token is present. Mirrors the old "auth required"
 * banner but as a centered card. A bad token surfaces via `authError` from
 * the WS `auth_ok`/error handshake, which re-shows this gate.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const authError = useConnectionStore((s) => s.authError)
  const [value, setValue] = useState('')
  const [token, setLocalToken] = useState(hasToken())

  if (token && !authError) return <>{children}</>

  const submit = () => {
    const v = value.trim()
    if (!v) return
    setToken(v)
    arcSocket.reauth(v)
    setLocalToken(true)
  }

  return (
    <div className="flex h-screen items-center justify-center bg-background p-6">
      <div className="w-full max-w-sm rounded-xl border border-border bg-card p-8 shadow-lg">
        <div className="mb-5 flex items-center gap-2">
          <ArcLogo />
          <span className="text-lg font-bold tracking-wide text-foreground">ARC</span>
        </div>
        <h1 className="text-base font-semibold text-foreground">
          {authError ? 'Token rejected' : 'Authentication required'}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Paste a viewer or operator token to connect. The CLI prints these on
          <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">arc ui start</code>.
        </p>
        <div className="mt-5 flex flex-col gap-2">
          <Input
            type="password"
            autoFocus
            placeholder="viewer token"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
          />
          <Button onClick={submit} disabled={!value.trim()}>
            Sign in
          </Button>
        </div>
      </div>
    </div>
  )
}
