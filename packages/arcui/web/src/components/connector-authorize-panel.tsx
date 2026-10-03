import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, HelpCircle, LogIn } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError } from '@/lib/api'
import { FieldHelp } from '@/components/help'
import { useAuthorizeConnector, useConnectorAuthStatus } from '@/lib/queries'

/**
 * Sign-in for a connector whose host program keeps its own credential, so there is
 * no stored field to edit. The customer finishes it here: paste a token and Arc
 * hands it to the program on its standard input (it keeps no copy). This shows
 * whether the program is signed in and the one action that changes it.
 *
 * It never shows a command to copy. A sign-in that only works in a terminal cannot
 * be finished from the browser, and the panel says that in words.
 */
export function ConnectorAuthorizePanel({
  instance,
  extension,
  operatorMode,
}: {
  instance: string
  extension: string
  operatorMode: boolean
}) {
  const queryClient = useQueryClient()
  const status = useConnectorAuthStatus(instance, true)
  const authorize = useAuthorizeConnector(instance)
  const [token, setToken] = useState('')

  const live = authorize.data ?? status.data
  // The server's authorisation CHECK, never its probe: a program that starts is
  // not a program that is signed in, and drawing the probe here is what told an
  // operator their Dropbox was connected when it was not.
  const signIn = live?.sign_in ?? 'unknown'
  const signedIn = signIn === 'signed_in'
  // The server names a command only when Arc cannot finish the sign-in itself, that
  // is, when the program's login asks questions that need a terminal.
  const needsTerminal = Boolean(authorize.data?.command ?? status.data?.command)

  const unreachable =
    status.isError &&
    (status.error instanceof ApiError && status.error.status === 404
      ? 'This copy of Arc cannot check the sign-in from here.'
      : status.error.message)

  const signInWithToken = () =>
    authorize.mutate(
      { token: token.trim() },
      {
        onSuccess: () => {
          setToken('')
          // The server re-checked the connection; the card must re-read its status.
          queryClient.invalidateQueries({ queryKey: ['connections'] })
        },
      },
    )

  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/20 p-3 text-xs">
      <div>
        <p className="font-medium text-foreground">Sign in to {extension}</p>
        <p className="mt-1 text-muted-foreground">
          {extension} keeps its own sign-in. Paste an access token and Arc passes it straight to
          the {extension} program on this computer. Arc does not keep a copy.
        </p>
      </div>

      {status.isLoading ? (
        <p className="text-muted-foreground">Checking the sign-in…</p>
      ) : unreachable ? (
        <p className="text-muted-foreground">{unreachable}</p>
      ) : signedIn ? (
        <p className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-2 text-emerald-700 dark:text-emerald-400">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
          <span>Signed in{live?.detail ? ` — ${live.detail}` : '.'}</span>
        </p>
      ) : signIn === 'signed_out' ? (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-amber-800 dark:text-amber-300">
          Not signed in yet{live?.detail ? ` — ${live.detail}` : '.'}
        </p>
      ) : (
        <p className="flex items-start gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-2 text-muted-foreground">
          <HelpCircle className="mt-0.5 size-4 shrink-0" />
          <span>
            Arc cannot tell whether {extension} is signed in — this program offers no way to
            check. If you have already signed it in, it is working.
            {live?.detail ? ` (${live.detail})` : ''}
          </span>
        </p>
      )}

      {needsTerminal ? (
        <p className="rounded-md border border-border bg-muted/40 px-2.5 py-2 text-muted-foreground">
          Signing in to {extension} asks questions that only work on the computer running Arc, so
          Arc cannot finish it from this page.
        </p>
      ) : operatorMode ? (
        <div className="space-y-1.5">
          <Input
            id={`connector-token-${instance}`}
            aria-label="Access token"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="••••••••"
          />
          <FieldHelp helpKey="connection.access_token" route="connections" />
          <Button
            size="sm"
            disabled={authorize.isPending || token.trim() === ''}
            onClick={signInWithToken}
          >
            <LogIn /> {authorize.isPending ? 'Signing in…' : signedIn ? 'Sign in again' : 'Sign in'}
          </Button>
        </div>
      ) : (
        <p className="italic text-muted-foreground">Turn on operator mode to sign in from here.</p>
      )}

      {authorize.isError && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-destructive">
          {authorize.error instanceof ApiError && authorize.error.status === 404
            ? 'This copy of Arc cannot run the sign-in for you.'
            : authorize.error.message}
        </p>
      )}
    </div>
  )
}
