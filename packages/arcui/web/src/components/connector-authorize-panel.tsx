import { useState } from 'react'
import { CheckCircle2, KeyRound, LogIn } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { InstructionBlock } from '@/components/instruction-block'
import { ApiError } from '@/lib/api'
import { useAuthorizeConnector, useConnectorAuthStatus } from '@/lib/queries'

/**
 * Sign-in for a connector that declares no secrets — the host binary holds its
 * own credential, so there is nothing for a person to paste and an empty
 * credential form would be a lie. This shows whether that binary is signed in
 * and offers the one action that changes it.
 */
export function ConnectorAuthorizePanel({
  agentId,
  instance,
  extension,
  operatorMode,
}: {
  agentId: string
  instance: string
  extension: string
  operatorMode: boolean
}) {
  const status = useConnectorAuthStatus(agentId, instance, true)
  const authorize = useAuthorizeConnector(agentId, instance)
  const [token, setToken] = useState('')
  const [showToken, setShowToken] = useState(false)

  const live = authorize.data ?? status.data
  const authorized = live?.authorized === true
  // Whatever the server last said a person must type, from either call.
  const command = authorize.data?.command ?? status.data?.command

  const unreachable =
    status.isError &&
    (status.error instanceof ApiError && status.error.status === 404
      ? 'This copy of Arc cannot check the sign-in from here.'
      : status.error.message)

  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/20 p-3 text-xs">
      <div>
        <p className="font-medium text-foreground">
          {extension} keeps its own sign-in — there is no password to enter here.
        </p>
        <p className="mt-1 text-muted-foreground">
          Arc uses the account the {extension} program on this computer is already signed in to.
        </p>
      </div>

      {status.isLoading ? (
        <p className="text-muted-foreground">Checking the sign-in…</p>
      ) : unreachable ? (
        <p className="text-muted-foreground">{unreachable}</p>
      ) : authorized ? (
        <p className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-2 text-emerald-700 dark:text-emerald-400">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
          <span>Signed in{live?.detail ? ` — ${live.detail}` : '.'}</span>
        </p>
      ) : (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-2 text-amber-800 dark:text-amber-300">
          Not signed in yet{live?.detail ? ` — ${live.detail}` : '.'}
        </p>
      )}

      {operatorMode ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={authorize.isPending}
              onClick={() => authorize.mutate(token.trim() ? { token: token.trim() } : {})}
            >
              <LogIn /> {authorize.isPending ? 'Signing in…' : authorized ? 'Sign in again' : 'Authorise'}
            </Button>
            <Button variant="ghost" size="xs" onClick={() => setShowToken(!showToken)}>
              <KeyRound /> {showToken ? 'Hide token box' : 'I have a token to paste'}
            </Button>
          </div>
          {showToken && (
            <div className="space-y-1.5">
              <Input
                id={`connector-token-${instance}`}
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="••••••••"
              />
              <p className="text-[11px] text-muted-foreground">
                Optional. Only some programs accept a token this way; leave it empty and Arc will
                try the normal sign-in.
              </p>
            </div>
          )}
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

      {!authorized && command && (
        <div className="space-y-1.5">
          <p className="text-muted-foreground">
            This sign-in asks questions that only work in a terminal window, so Arc cannot finish it
            for you. Someone with access to the computer running Arc can type this:
          </p>
          <InstructionBlock text={command} />
        </div>
      )}
    </div>
  )
}
