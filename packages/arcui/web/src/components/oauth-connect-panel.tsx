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
import type { OAuthAppBody, OAuthAppResponse, OAuthBeginResponse } from '@/lib/types'

/** Where each provider's step-by-step setup lives in the operator runbooks. */
function runbookFor(provider: string): string {
  if (provider === 'microsoft') return 'docs/runbooks/operate/connections.md (Microsoft 365)'
  return 'docs/runbooks/operate/google-accounts.md'
}

/** The Microsoft Graph delegated permissions the app registration must list. */
const MICROSOFT_PERMISSIONS = [
  'openid',
  'profile',
  'offline_access',
  'User.Read',
  'Mail.Read',
  'Mail.Send',
  'Calendars.ReadWrite',
  'Files.Read',
]
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
  const [tenantId, setTenantId] = useState(app.tenant_id ?? '')
  const clouds = app.clouds ?? []
  const [cloud, setCloud] = useState(app.cloud || clouds[0]?.id || '')
  const label = providerLabel(provider)
  const needsTenant = app.tenant_required === true
  const ready = clientId.trim() !== '' && clientSecret !== '' && (!needsTenant || tenantId.trim() !== '')

  const body = (): OAuthAppBody => {
    const base: OAuthAppBody = { client_id: clientId.trim(), client_secret: clientSecret }
    if (needsTenant) base.tenant_id = tenantId.trim()
    if (clouds.length > 0) base.cloud = cloud
    return base
  }

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
      {provider === 'microsoft' && <EntraSteps />}
      <p className="text-muted-foreground">
        Step by step: <span className="font-mono text-foreground">{runbookFor(provider)}</span>
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
        {needsTenant && (
          <Input
            aria-label="Tenant ID"
            autoComplete="off"
            spellCheck={false}
            value={tenantId}
            onChange={(e) => setTenantId(e.target.value)}
            placeholder="Directory (tenant) ID, like 11111111-2222-3333-4444-555555555555"
          />
        )}
        <Input
          aria-label="Client secret"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder="••••••••"
        />
        {clouds.length > 0 && (
          <label className="flex items-center gap-2 text-muted-foreground">
            Cloud
            <select
              aria-label="Cloud"
              className="h-8 rounded-md border border-border bg-background px-2 text-foreground"
              value={cloud}
              onChange={(e) => setCloud(e.target.value)}
            >
              {clouds.map((choice) => (
                <option key={choice.id} value={choice.id}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <Button
        size="sm"
        disabled={save.isPending || !ready}
        onClick={() => save.mutate(body(), { onSuccess: () => setClientSecret('') })}
      >
        {save.isPending ? 'Saving…' : 'Save app'}
      </Button>
      {save.isError && <p className={ERROR_BOX}>{save.error.message}</p>}
    </div>
  )
}

/**
 * What to create in Microsoft Entra, in plain words. Shown above the form so an
 * operator who has never registered an app can do it without leaving the page.
 */
function EntraSteps() {
  return (
    <ol data-entra-steps className="list-decimal space-y-1 pl-4 text-muted-foreground">
      <li>
        In the Microsoft Entra admin center, open App registrations and click New registration.
        Choose &quot;Accounts in this organizational directory only&quot;.
      </li>
      <li>
        Under Redirect URI pick the <span className="text-foreground">Web</span> platform and paste the
        redirect address above. The portal does not accept an http://127.0.0.1 address in that box: add
        it in the app&apos;s Manifest (replyUrlsWithType, type Web) instead.
      </li>
      <li>Under Certificates &amp; secrets, create a client secret and copy its Value.</li>
      <li>
        Under API permissions, add Microsoft Graph delegated permissions:{' '}
        <span className="font-mono text-foreground">{MICROSOFT_PERMISSIONS.join(', ')}</span>.
      </li>
      <li>
        Click <span className="text-foreground">Grant admin consent</span>. Government (GCC) tenants
        usually block users from consenting themselves, so an admin must do this once.
      </li>
      <li>
        Copy the Application (client) ID and the Directory (tenant) ID from the Overview page into the
        boxes below. Pick Commercial / GCC unless your tenant is GCC High or DoD.
      </li>
    </ol>
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
