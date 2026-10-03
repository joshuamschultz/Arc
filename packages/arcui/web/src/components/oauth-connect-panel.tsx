import { useState } from 'react'
import { CheckCircle2, ChevronRight, ExternalLink, LogIn } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { CopyButton } from '@/components/copy-button'
import {
  useBeginOAuth,
  useCompleteOAuth,
  useOAuthApp,
  useOAuthConnectedListener,
  useSetOAuthApp,
} from '@/lib/queries'
import type { OAuthAppResponse, OAuthBeginResponse } from '@/lib/types'

const RUNBOOK = 'docs/runbooks/operate/google-accounts.md'
const ERROR_BOX =
  'rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-destructive'

/** What a person calls a provider; the id is the fallback, capitalised. */
function providerLabel(provider: string): string {
  const known: Record<string, string> = {
    google: 'Google',
    dropbox: 'Dropbox',
    atlassian: 'Atlassian',
    slack: 'Slack',
    microsoft: 'Microsoft',
  }
  return known[provider] ?? provider.charAt(0).toUpperCase() + provider.slice(1)
}

/** Only an https address becomes a link; provider-supplied text is never markup. */
function isHttps(url: string): boolean {
  return url.startsWith('https://')
}

/**
 * Arc has no OAuth app for this provider yet. The person registers one with the
 * provider (the redirect address below goes into that registration), then pastes
 * its client id and secret here. The secret goes to the server once and is never
 * shown again.
 */
function AppSetupPanel({ provider, app }: { provider: string; app: OAuthAppResponse }) {
  const save = useSetOAuthApp(provider)
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const label = providerLabel(provider)

  return (
    <div data-oauth-app-setup className="space-y-3 rounded-md border border-border bg-muted/20 p-3 text-xs">
      <p className="font-medium text-foreground">Set up the {label} app once, then connect in one click.</p>
      <div className="space-y-1">
        <p className="text-muted-foreground">Register this redirect address with {label}:</p>
        <div className="flex items-center gap-1">
          <Input readOnly aria-label="Redirect address" value={app.redirect_uri} />
          <CopyButton text={app.redirect_uri} />
        </div>
      </div>
      <p className="text-muted-foreground">
        Step by step: <span className="font-mono text-foreground">{RUNBOOK}</span>
        {isHttps(app.console_url) && (
          <>
            {' · '}
            <a
              href={app.console_url}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-1 font-medium text-foreground underline"
            >
              {label} developer console <ExternalLink className="size-3" />
            </a>
          </>
        )}
      </p>
      <div className="space-y-1.5">
        <Input
          aria-label="Client ID"
          autoComplete="off"
          spellCheck={false}
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder="Client ID"
        />
        <Input
          aria-label="Client secret"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder="••••••••"
        />
      </div>
      <Button
        size="sm"
        disabled={save.isPending || !clientId.trim() || !clientSecret}
        onClick={() =>
          save.mutate(
            { client_id: clientId.trim(), client_secret: clientSecret },
            { onSuccess: () => setClientSecret('') },
          )
        }
      >
        {save.isPending ? 'Saving…' : 'Save app'}
      </Button>
      {save.isError && <p className={ERROR_BOX}>{save.error.message}</p>}
    </div>
  )
}

/** Shown after the provider page is opened: wait, or paste back by hand. */
function WaitingPanel({
  label,
  begin,
  onFinished,
}: {
  label: string
  begin: OAuthBeginResponse
  onFinished: () => void
}) {
  const complete = useCompleteOAuth()
  const [entry, setEntry] = useState('')
  const byCode = begin.redirect_mode === 'none'

  const finish = () =>
    complete.mutate(
      byCode ? { state: begin.state, code: entry.trim() } : { redirect_url: entry.trim() },
      { onSuccess: onFinished },
    )

  return (
    <div className="space-y-2">
      <p className="text-muted-foreground">Waiting for {label}…</p>
      <details className="rounded-md border border-border bg-background px-2.5 py-2" open={byCode}>
        <summary className="flex cursor-pointer items-center gap-1 font-medium text-foreground">
          <ChevronRight className="size-3.5" />
          {byCode ? `Paste the code ${label} shows` : "Didn't come back? Paste the address you landed on"}
        </summary>
        <div className="mt-2 space-y-2">
          <Input
            aria-label={byCode ? `Code from ${label}` : 'Address you landed on'}
            autoComplete="off"
            spellCheck={false}
            value={entry}
            onChange={(e) => setEntry(e.target.value)}
            placeholder={byCode ? 'Code' : 'https://…/oauth/callback?…'}
          />
          <Button size="sm" disabled={complete.isPending || !entry.trim()} onClick={finish}>
            <CheckCircle2 /> {complete.isPending ? 'Finishing…' : 'Finish connecting'}
          </Button>
          {complete.isError && <p className={ERROR_BOX}>{complete.error.message}</p>}
        </div>
      </details>
    </div>
  )
}

/**
 * One-click connect for a connection that signs in with OAuth. If Arc has no app
 * for the provider it asks for one first; otherwise a single button opens the
 * provider's consent page in a new tab and waits for the callback tab to say it
 * is done (or for the person to paste the address back).
 */
export function OAuthConnectPanel({
  instance,
  provider,
  reconnect,
  onDone,
}: {
  /** The connection being connected: the begin call is keyed by it. */
  instance: string
  provider: string
  reconnect: boolean
  /** Closes the panel once the connection is made. */
  onDone: () => void
}) {
  const app = useOAuthApp(provider, true)
  const begin = useBeginOAuth(instance)
  const label = providerLabel(provider)
  useOAuthConnectedListener(onDone)

  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/20 p-3 text-xs">
      {app.isLoading && <p className="text-muted-foreground">Checking the {label} app…</p>}
      {app.isError && <p className={ERROR_BOX}>{app.error.message}</p>}
      {app.data && !app.data.configured && <AppSetupPanel provider={provider} app={app.data} />}
      {app.data?.configured && (
        <>
          <Button
            size="sm"
            disabled={begin.isPending}
            onClick={() =>
              begin.mutate(undefined, {
                onSuccess: (started) => window.open(started.authorize_url, '_blank', 'noopener'),
              })
            }
          >
            <LogIn /> {begin.isPending ? 'Starting…' : reconnect ? 'Reconnect' : 'Connect'}
          </Button>
          {begin.isError && <p className={ERROR_BOX}>{begin.error.message}</p>}
          {begin.data && <WaitingPanel label={label} begin={begin.data} onFinished={onDone} />}
        </>
      )}
    </div>
  )
}
