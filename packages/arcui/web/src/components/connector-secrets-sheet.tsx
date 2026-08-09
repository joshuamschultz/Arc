import { useEffect, useState } from 'react'
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
import {
  useConnectorAuthorization,
  useInstallConnector,
  useReauthConnector,
} from '@/lib/queries'
import { ApiError } from '@/lib/api'
import { asUnsatisfiedHost, type CatalogBundle, type HostRequirement } from '@/lib/types'

// A connector's connect form: one input per declared field. The bundle says which
// of them are credentials, and only those are masked — a base URL rendered as
// password dots protects nothing and hides the one thing an operator needs to
// check. A sensitive value is never rendered back, put in a URL, or kept after the
// request lands; a non-sensitive one may be shown, because it is not a secret.
export function ConnectorSecretsSheet({
  agentId,
  bundle,
  instance,
  open,
  onOpenChange,
}: {
  agentId: string
  bundle: CatalogBundle
  /** Set for a rotation; omitted when installing a new instance. */
  instance?: string
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  // Both mutations are constructed (rules of hooks); `instance` picks which
  // one actually runs.
  const install = useInstallConnector(agentId)
  const reauth = useReauthConnector(agentId, instance ?? '')
  const [name, setName] = useState(instance ?? '')
  const [values, setValues] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [unsatisfied, setUnsatisfied] = useState<HostRequirement[]>([])
  const [operatorMode] = useOperatorMode()

  const rotating = instance !== undefined
  const busy = install.isPending || reauth.isPending
  const complete = bundle.secrets.every((s) => (values[s.name] ?? '').length > 0)
  const canSubmit = complete && (rotating || name.trim().length > 0) && !busy

  // On a rotation, fill the non-sensitive fields with what is already configured.
  // Only those come back with a value — a credential is never read out of the
  // store — so this can prefill a base URL and can never prefill a token. Without
  // it an operator rotating a token must retype a URL they cannot see and never
  // changed, and one typo in it fails the probe with nothing to look at.
  const configured = useConnectorAuthorization(agentId, instance ?? '', rotating && open)
  useEffect(() => {
    const known = configured.data?.credentials
    if (!known) return
    setValues((current) => {
      const seeded = { ...current }
      for (const field of known) {
        if (!field.sensitive && field.value && seeded[field.name] === undefined) {
          seeded[field.name] = field.value
        }
      }
      return seeded
    })
  }, [configured.data])

  const clear = () => {
    setValues({})
    setError(null)
    setUnsatisfied([])
  }

  const handleOpenChange = (o: boolean) => {
    onOpenChange(o)
    if (!o) {
      clear()
      setName(instance ?? '')
    }
  }

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
    if (rotating) {
      reauth.mutate(values, { onSuccess: done, onError: fail })
      return
    }
    install.mutate(
      { extension: bundle.name, instance: name.trim(), secrets: values },
      { onSuccess: done, onError: fail },
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
            {rotating ? `Re-authenticate ${instance}` : `Connect ${bundle.name}`}
          </SheetTitle>
          <SheetDescription>
            {rotating
              ? `Supply fresh credentials for ${bundle.name}. The old ones are replaced.`
              : bundle.description}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {(unsatisfied.length > 0 || bundle.host_requires.length > 0) && (
            <HostSetupPanel
              agentId={agentId}
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
                How this agent refers to this account of {bundle.name}. One agent can hold
                several.
              </p>
            </div>
          )}
          {bundle.secrets.map((s) => (
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
                value={values[s.name] ?? ''}
                onChange={(e) => setValues({ ...values, [s.name]: e.target.value })}
                placeholder={s.sensitive ? '••••••••' : 'https://…'}
              />
              <p className="text-[11px] text-muted-foreground">{s.prompt}</p>
            </div>
          ))}
          {bundle.secrets.length === 0 && (
            <p className="rounded-md border border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
              There is nothing to type here. {bundle.name} keeps its own sign-in on this computer,
              so Arc just points at it. Use{' '}
              <span className="font-medium text-foreground">Sign in</span> on the connection row to
              check or renew that sign-in.
            </p>
          )}
          <Button className="w-full" disabled={!canSubmit} onClick={submit}>
            {busy ? 'Working…' : rotating ? 'Replace credentials' : 'Connect'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}
