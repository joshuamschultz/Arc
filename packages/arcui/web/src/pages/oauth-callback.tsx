import { useEffect, useRef, useState } from 'react'
import { CheckCircle2 } from 'lucide-react'
import { ArcLogo } from '@/components/arc-logo'
import { CopyButton } from '@/components/copy-button'
import { Input } from '@/components/ui/input'
import { apiPost } from '@/lib/api'
import { hasToken } from '@/lib/auth'
import { OAUTH_CHANNEL } from '@/lib/queries'

type Outcome =
  | { kind: 'working' }
  | { kind: 'done' }
  | { kind: 'error'; message: string }
  // No sign-in in this browser: the address is shown so a person can paste it into
  // the Arc tab that started the connect.
  | { kind: 'no_session'; address: string }

function announceConnected(): void {
  if (typeof BroadcastChannel === 'undefined') return
  const channel = new BroadcastChannel(OAUTH_CHANNEL)
  channel.postMessage({ type: 'connected' })
  channel.close()
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'Could not finish the connection.'
}

/**
 * Where a provider sends the browser after the person clicks Allow. The address
 * carries a one-time code, so it is read once, dropped from the address bar at
 * once (history, screenshots and the Referer header never see it), and sent to the
 * server a single time.
 */
export function OAuthCallbackPage() {
  // Read before the effect strips it. A lazy initialiser only reads; it never writes.
  const [captured] = useState(() => window.location.href)
  const [outcome, setOutcome] = useState<Outcome>(() =>
    hasToken() ? { kind: 'working' } : { kind: 'no_session', address: captured },
  )
  // StrictMode runs effects twice; the code is single-use, so it is posted once.
  const started = useRef(false)

  useEffect(() => {
    if (started.current) return
    started.current = true
    window.history.replaceState(null, '', window.location.pathname)
    if (!hasToken()) return
    apiPost('/api/oauth/complete', { redirect_url: captured })
      .then(() => {
        announceConnected()
        setOutcome({ kind: 'done' })
      })
      .catch((error: unknown) => setOutcome({ kind: 'error', message: errorMessage(error) }))
  }, [captured])

  return (
    <div className="flex h-dvh items-center justify-center bg-background p-4 md:p-6">
      <div className="w-full max-w-md space-y-4 rounded-xl border border-border bg-card p-8 shadow-lg">
        <ArcLogo />
        <CallbackBody outcome={outcome} />
      </div>
    </div>
  )
}

function CallbackBody({ outcome }: { outcome: Outcome }) {
  switch (outcome.kind) {
    case 'working':
      return <p className="text-sm text-muted-foreground">Finishing the connection…</p>
    case 'done':
      return (
        <p className="flex items-start gap-2 text-sm text-emerald-700 dark:text-emerald-400">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
          <span>Connected. You can close this tab.</span>
        </p>
      )
    case 'error':
      return (
        <p role="alert" className="text-sm text-destructive">
          {outcome.message}
        </p>
      )
    case 'no_session':
      return (
        <div className="space-y-2 text-sm">
          <p className="text-foreground">
            Paste this page&apos;s address into the Arc tab that started the sign-in.
          </p>
          <div className="flex items-center gap-1">
            <Input readOnly aria-label="This page's address" value={outcome.address} />
            <CopyButton text={outcome.address} />
          </div>
        </div>
      )
  }
}
