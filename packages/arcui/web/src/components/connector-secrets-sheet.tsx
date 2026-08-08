import { useState } from 'react'
import { TriangleAlert } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useInstallConnector, useReauthConnector } from '@/lib/queries'
import { ApiError } from '@/lib/api'
import { asUnsatisfiedHost, type CatalogBundle, type HostRequirement } from '@/lib/types'

// A connector's credentials, one masked field per declared secret. Every input
// is a password field with autocomplete off; no value is ever rendered back,
// put in a URL, or kept after the request lands.
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

  const rotating = instance !== undefined
  const busy = install.isPending || reauth.isPending
  const complete = bundle.secrets.every((s) => (values[s.name] ?? '').length > 0)
  const canSubmit = complete && (rotating || name.trim().length > 0) && !busy

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
          {unsatisfied.length > 0 && (
            <div className="space-y-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
              <p className="flex items-start gap-2">
                <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                <span>
                  This host is missing something {bundle.name} needs. Install it yourself, then
                  connect again — arcui will not install it for you.
                </span>
              </p>
              {unsatisfied.map((h) => (
                <div key={h.name} className="space-y-1">
                  <div className="font-mono text-[11px] text-foreground">{h.name}</div>
                  <pre className="overflow-x-auto rounded border border-border bg-muted/40 px-2 py-1 font-mono text-[11px] text-foreground">
                    {h.instruction}
                  </pre>
                </div>
              ))}
            </div>
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
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={values[s.name] ?? ''}
                onChange={(e) => setValues({ ...values, [s.name]: e.target.value })}
                placeholder="••••••••"
              />
              <p className="text-[11px] text-muted-foreground">{s.prompt}</p>
            </div>
          ))}
          {bundle.host_requires.length > 0 && unsatisfied.length === 0 && (
            <div className="space-y-1 rounded-md border border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
              <p className="font-medium text-foreground">Needs on this host</p>
              {bundle.host_requires.map((h) => (
                <p key={h.name}>
                  <span className="font-mono text-foreground">{h.name}</span> — {h.instruction}
                </p>
              ))}
            </div>
          )}
          <Button className="w-full" disabled={!canSubmit} onClick={submit}>
            {busy ? 'Working…' : rotating ? 'Replace credentials' : 'Connect'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}
