import { useState } from 'react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { HostSetupPanel } from '@/components/host-setup-panel'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useConnectorAuthorization, useInstallConnector, useReauthConnector } from '@/lib/queries'
import type { ConnectorProbeResponse } from '@/lib/types'
import { agentLabel, grantName } from '@/lib/agent-names'
import { ApiError, apiPost } from '@/lib/api'
import {
  asUnsatisfiedHost,
  type Agent,
  type CatalogBundle,
  type HostRequirement,
} from '@/lib/types'
import { cn } from '@/lib/utils'

// A connector's connect form: one input per declared field, plus the question a
// connection is useless without — who gets to use it. The bundle says which
// fields are credentials, and only those are masked — a base URL rendered as
// password dots protects nothing and hides the one thing an operator needs to
// check. A sensitive value is never rendered back, put in a URL, or kept after the
// request lands; a non-sensitive one may be shown, because it is not a secret.
export function ConnectorSecretsSheet({
  bundle,
  instance,
  agents,
  open,
  onOpenChange,
}: {
  bundle: CatalogBundle
  /** Set for a rotation; omitted when installing a new instance. */
  instance?: string
  /** The fleet, so the connect form can ask who this account is for. */
  agents: Agent[]
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  // Both mutations are constructed (rules of hooks); `instance` picks which
  // one actually runs.
  const install = useInstallConnector()
  const reauth = useReauthConnector(instance ?? '')
  const [name, setName] = useState(instance ?? '')
  const [values, setValues] = useState<Record<string, string>>({})
  const [granted, setGranted] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [unsatisfied, setUnsatisfied] = useState<HostRequirement[]>([])
  const [operatorMode] = useOperatorMode()

  const [okMsg, setOkMsg] = useState<string | null>(null)
  const [verifying, setVerifying] = useState(false)
  const rotating = instance !== undefined
  const busy = install.isPending || reauth.isPending || verifying

  // On a rotation, show the non-sensitive fields as already configured. Only
  // those come back with a value — a credential is never read out of the store —
  // so this can fill in a base URL and can never fill in a token. Without it an
  // operator rotating a token must retype a URL they cannot see and never
  // changed, and one typo in it fails the probe with nothing to look at.
  //
  // Derived rather than copied into state: `values` holds what the operator
  // TYPED, and everything else falls through to what the server says is
  // configured. Seeding state from the response instead meant one render with
  // empty inputs and a second that replaced them, which is a cascade the moment
  // the query refetches under a half-typed form.
  const configured = useConnectorAuthorization(instance ?? '', rotating && open)
  const stored = (field: string) => {
    const known = configured.data?.credentials.find((c) => c.name === field)
    return known && !known.sensitive ? known.value : ''
  }
  const valueFor = (field: string) => values[field] ?? stored(field)
  // An OAuth connector's refresh token is obtained by the Connect (code-exchange)
  // flow, never typed here — the server's credential list already omits it, so a
  // rotating OAuth form asks only for the app key/secret. Fall back to every
  // declared secret when the server has not answered (a fresh, non-rotating form).
  const authNames = configured.data?.oauth
    ? new Set((configured.data.credentials ?? []).map((c) => c.name))
    : null
  const fields = authNames ? bundle.secrets.filter((s) => authNames.has(s.name)) : bundle.secrets
  const submitted = () => Object.fromEntries(fields.map((s) => [s.name, valueFor(s.name)]))

  const complete = fields.every((s) => valueFor(s.name).length > 0)
  const canSubmit = complete && (rotating || name.trim().length > 0) && !busy

  const clear = () => {
    setValues({})
    setError(null)
    setUnsatisfied([])
    setOkMsg(null)
  }

  // Writing a credential and reporting nothing is what made a good save look
  // like a reset: the panel closed, the card status never re-ran, and the
  // operator could not tell a working token from a dead one. So after the
  // credential is stored, probe the connection live and show the answer —
  // "reached Slack as …" or the exact failure (a missing scope, a bad token) —
  // instead of silently closing.
  const verify = async (inst: string) => {
    setVerifying(true)
    try {
      const result = await apiPost<ConnectorProbeResponse>(
        `/api/connections/${encodeURIComponent(inst)}/probe`,
      )
      if (result.reachable) {
        setOkMsg(result.detail || 'Connected.')
      } else {
        setError(
          result.detail ||
            'Saved, but the connection did not answer. Check the credential and its scopes.',
        )
      }
    } catch {
      // The credential is stored; only the live check could not run.
      setOkMsg('Saved. A live check could not be run right now.')
    } finally {
      setVerifying(false)
    }
  }

  const handleOpenChange = (o: boolean) => {
    onOpenChange(o)
    if (!o) {
      clear()
      setName(instance ?? '')
      setGranted([])
    }
  }

  const toggleAgent = (agent: string) =>
    setGranted((current) =>
      current.includes(agent) ? current.filter((n) => n !== agent) : [...current, agent],
    )

  const fail = (e: Error) => {
    setError(e.message)
    setUnsatisfied(e instanceof ApiError ? asUnsatisfiedHost(e.body) : [])
  }

  const done = () => {
    clear()
    onOpenChange(false)
  }

  const submit = () => {
    setError(null)
    setUnsatisfied([])
    setOkMsg(null)
    if (rotating && instance) {
      reauth.mutate(submitted(), { onSuccess: () => verify(instance), onError: fail })
      return
    }
    const newInstance = name.trim()
    install.mutate(
      {
        extension: bundle.name,
        instance: newInstance,
        agents: granted,
        secrets: submitted(),
      },
      { onSuccess: () => verify(newInstance), onError: fail },
    )
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-md"
      >
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">
            {rotating ? `Re-authenticate ${instance}` : `Connect ${bundle.display_name}`}
          </SheetTitle>
          <SheetDescription>
            {rotating
              ? `Supply fresh credentials for ${bundle.display_name}. The old ones are replaced.`
              : bundle.description}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {okMsg && (
            <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-400">
              {okMsg}
            </div>
          )}
          {(unsatisfied.length > 0 || bundle.host_requires.length > 0) && (
            <HostSetupPanel
              extension={bundle.name}
              requirements={unsatisfied.length > 0 ? unsatisfied : bundle.host_requires}
              operatorMode={operatorMode}
              blocking={unsatisfied.length > 0}
            />
          )}
          {!rotating && (
            <div className="space-y-1.5">
              <label
                htmlFor="connector-instance"
                className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground"
              >
                Instance name
              </label>
              <Input
                id="connector-instance"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="work"
                autoComplete="off"
              />
              <p className="text-[11px] text-muted-foreground">
                What this account of {bundle.display_name} is called. One deployment can hold several.
              </p>
            </div>
          )}
          {!rotating && (
            // Asked here, on the way in, rather than left as a second step an
            // operator has to know exists. Deny by default means a connection
            // handed to nobody works perfectly and serves nobody, and the person
            // most likely to meet that state is the one who did not know there
            // was a question.
            <div className="space-y-1.5">
              <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
                Who can use it
              </span>
              <div className="flex flex-wrap gap-1.5">
                {agents.map((agent) => {
                  const key = grantName(agent)
                  const picked = granted.includes(key)
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => toggleAgent(key)}
                      className={cn(
                        'rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors',
                        picked
                          ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                          : 'border-dashed border-border text-muted-foreground hover:border-foreground/40',
                      )}
                    >
                      {agentLabel(agent)}
                    </button>
                  )
                })}
                {agents.length === 0 && (
                  <p className="text-[11px] text-muted-foreground">
                    No agents on this deployment yet.
                  </p>
                )}
              </div>
              <p
                className={cn(
                  'text-[11px]',
                  granted.length === 0
                    ? 'text-amber-700 dark:text-amber-400'
                    : 'text-muted-foreground',
                )}
              >
                {granted.length === 0
                  ? 'Nobody selected — this will connect and no agent will be able to use it. You can grant it later.'
                  : 'Only these agents get its tools. You can change this at any time.'}
              </p>
            </div>
          )}
          {fields.map((s) => (
            <div key={s.name} className="space-y-1.5">
              <label
                htmlFor={`connector-secret-${s.name}`}
                className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground"
              >
                {s.name}
              </label>
              <Input
                id={`connector-secret-${s.name}`}
                type={s.sensitive ? 'password' : 'text'}
                autoComplete="off"
                spellCheck={false}
                value={valueFor(s.name)}
                onChange={(e) => setValues({ ...values, [s.name]: e.target.value })}
                placeholder={s.sensitive ? '••••••••' : 'https://…'}
              />
              <p className="text-[11px] text-muted-foreground">{s.prompt}</p>
            </div>
          ))}
          {bundle.secrets.length === 0 && (
            <p className="rounded-md border border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
              There is nothing to type here. {bundle.display_name} keeps its own sign-in on this computer,
              so Arc just points at it. Use{' '}
              <span className="font-medium text-foreground">Sign in</span> on the connection row to
              check or renew that sign-in.
            </p>
          )}
          {okMsg ? (
            <Button className="w-full" variant="outline" onClick={done}>
              Done
            </Button>
          ) : (
            <Button className="w-full" disabled={!canSubmit} onClick={submit}>
              {busy ? 'Working…' : rotating ? 'Replace credentials' : 'Connect'}
            </Button>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
