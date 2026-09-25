import { useState } from 'react'
import { CheckCircle2, ExternalLink, HelpCircle, LogIn, Pencil, TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { FieldHelp } from '@/components/help'
import { ApiError } from '@/lib/api'
import {
  useBeginSignIn,
  useCompleteSignIn,
  useConnectorAuthStatus,
  useConnectorAuthorization,
} from '@/lib/queries'
import type { ConnectorSignIn } from '@/lib/types'

/** What each sign-in state means to a person, in their words. */
const SIGN_IN_LABEL: Record<ConnectorSignIn, string> = {
  signed_in: 'Working',
  expired: 'Reconnect needed — Google stopped accepting the saved sign-in',
  signed_out: 'Not signed in yet',
  not_installed: 'The Google program (gog) is not installed on this computer',
  unknown: "Can't tell yet",
}

const TONE: Record<ConnectorSignIn, string> = {
  signed_in:
    'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  expired: 'border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-300',
  signed_out: 'border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-300',
  not_installed: 'border-destructive/30 bg-destructive/10 text-destructive',
  unknown: 'border-border bg-muted/40 text-muted-foreground',
}

function errorText(error: Error, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback
}

/** The sign-in state as one coloured line, with the server's detail beneath. */
function SignInState({ signIn, detail }: { signIn: ConnectorSignIn; detail?: string }) {
  const Icon =
    signIn === 'signed_in' ? CheckCircle2 : signIn === 'unknown' ? HelpCircle : TriangleAlert
  return (
    <div className={`rounded-md border px-2.5 py-2 ${TONE[signIn]}`}>
      <p className="flex items-start gap-2">
        <Icon className="mt-0.5 size-4 shrink-0" />
        <span>{SIGN_IN_LABEL[signIn]}</span>
      </p>
      {detail && <p className="mt-1 pl-6 text-[11px] text-muted-foreground">{detail}</p>}
    </div>
  )
}

/**
 * Reconnect a connection whose host program can finish its sign-in from the
 * browser (Google via gog). Arc cannot open a browser on the computer it runs
 * on, so the sign-in is split in two: Arc hands over Google's consent link, the
 * person allows access in their own browser, and pastes back the address that
 * browser lands on. The pasted address carries a one-time code — it is sent once,
 * never logged, and cleared the moment the sign-in succeeds.
 */
export function RemoteSignInPanel({
  instance,
  operatorMode,
  onEditDetails,
}: {
  instance: string
  operatorMode: boolean
  /** Opens the card's Edit details form (account, client, access). */
  onEditDetails?: () => void
}) {
  const authz = useConnectorAuthorization(instance, true)
  const status = useConnectorAuthStatus(instance, true)
  const begin = useBeginSignIn(instance)
  const complete = useCompleteSignIn(instance)
  const [address, setAddress] = useState('')

  const credentials = authz.data?.credentials ?? []
  const account = credentials.find((c) => c.name === 'account')?.value.trim() ?? ''
  // The server fills `warning` only for a field that is blank right now.
  const warnings = credentials.map((c) => c.warning ?? '').filter((w) => w.length > 0)
  const readOnly = credentials.find((c) => c.name === 'read_only')
  const readOnlyValue = readOnly ? readOnly.value || readOnly.default || 'yes' : null
  // The newest verified answer wins: a finished sign-in, then the live check,
  // then whatever the authorisation read carried.
  const live = complete.data ?? status.data
  const signIn: ConnectorSignIn = live?.sign_in ?? authz.data?.sign_in ?? 'unknown'
  const detail = live?.detail ?? authz.data?.detail
  const reconnect = signIn === 'expired' || signIn === 'signed_in'
  const started = begin.data

  const finish = () =>
    complete.mutate(
      { redirect_url: address.trim() },
      {
        onSuccess: () => {
          // The code in that address is spent: clear it and the old link.
          setAddress('')
          begin.reset()
        },
      },
    )

  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/20 p-3 text-xs">
      <div>
        <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Google account
          <FieldHelp helpKey="connection.google_account" route="connections" />
        </p>
        {account ? (
          <p className="mt-1 font-medium text-foreground">{account}</p>
        ) : authz.isLoading ? (
          <p className="mt-1 text-muted-foreground">Loading…</p>
        ) : (
          <p className="mt-1 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-amber-800 dark:text-amber-300">
            Set this connection&apos;s account (Edit details) before signing in.
          </p>
        )}
      </div>

      {readOnlyValue && (
        <p className="text-muted-foreground">
          {readOnlyValue === 'no' ? 'Access: read, draft and send' : 'Access: read only'}
          <FieldHelp helpKey="connection.google_access" route="connections" />
        </p>
      )}

      {authz.isLoading && status.isLoading ? (
        <p className="text-muted-foreground">Checking the sign-in…</p>
      ) : (
        <SignInState signIn={signIn} detail={detail} />
      )}

      {!operatorMode ? (
        <p className="italic text-muted-foreground">Turn on operator mode to sign in from here.</p>
      ) : (
        account && (
          <div className="space-y-3">
            <div className="space-y-2">
              <p className="font-medium text-foreground">Step 1</p>
              {warnings.length > 0 && !started && (
                <div className="space-y-1 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-amber-800 dark:text-amber-300">
                  {warnings.map((w) => (
                    <p key={w}>{w}</p>
                  ))}
                  <p>Set your own client with Edit details first, then sign in.</p>
                </div>
              )}
              <div className="flex flex-wrap items-center gap-1">
                {warnings.length > 0 && !started ? (
                  <>
                    {onEditDetails && (
                      <Button size="sm" onClick={onEditDetails}>
                        <Pencil /> Edit details
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={begin.isPending}
                      onClick={() => begin.mutate({ accept_warnings: true })}
                    >
                      <LogIn />
                      {begin.isPending ? 'Starting…' : 'Sign in with the built-in client anyway'}
                    </Button>
                  </>
                ) : (
                  <Button
                    size="sm"
                    disabled={begin.isPending}
                    // A warning accepted once stays accepted for a fresh link.
                    onClick={() => begin.mutate(warnings.length > 0 ? { accept_warnings: true } : {})}
                  >
                    <LogIn />
                    {begin.isPending
                      ? 'Starting…'
                      : started
                        ? 'Get a new link'
                        : reconnect
                          ? 'Reconnect'
                          : 'Open Google sign-in'}
                  </Button>
                )}
                <FieldHelp helpKey="connection.google_sign_in" route="connections" />
              </div>
              {begin.isError && (
                <p className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-destructive">
                  {errorText(begin.error, 'Could not start the sign-in.')}
                </p>
              )}
              {started && (
                <div className="space-y-1">
                  <a
                    href={started.consent_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 font-medium text-foreground hover:bg-muted"
                  >
                    <ExternalLink className="size-3.5" /> Open Google sign-in
                  </a>
                  <p className="text-muted-foreground">
                    The link expires in {Math.max(1, Math.ceil(started.expires_in / 60))} minutes.
                  </p>
                </div>
              )}
            </div>

            {started && (
              <div className="space-y-2">
                <p className="font-medium text-foreground">Step 2</p>
                <p className="text-muted-foreground">
                  Sign in as {account} and click Allow. You will land on a page that fails to load —
                  that is expected. Copy the whole address from the browser bar and paste it here.
                </p>
                <div className="flex items-center gap-1">
                  <Input
                    id={`remote-sign-in-address-${instance}`}
                    aria-label="Address from the browser bar"
                    autoComplete="off"
                    spellCheck={false}
                    value={address}
                    onChange={(e) => setAddress(e.target.value)}
                    placeholder="http://127.0.0.1:…/oauth2/callback?…"
                  />
                  <FieldHelp helpKey="connection.sign_in_address" route="connections" />
                </div>
                <Button
                  size="sm"
                  disabled={complete.isPending || !address.trim()}
                  onClick={finish}
                >
                  <CheckCircle2 /> {complete.isPending ? 'Finishing…' : 'Finish sign-in'}
                </Button>
              </div>
            )}

            {complete.isError && (
              <p className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-destructive">
                {errorText(complete.error, 'Could not finish the sign-in.')}
              </p>
            )}
            {/* A result that is not "signed in" is already drawn in the state line above. */}
            {complete.data?.sign_in === 'signed_in' && (
              <p className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-2 text-emerald-700 dark:text-emerald-400">
                <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
                <span>Working — signed in as {account}.</span>
              </p>
            )}
          </div>
        )
      )}
    </div>
  )
}
