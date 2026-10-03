import { useState, type ReactNode } from 'react'
import { Globe, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { CopyButton } from '@/components/copy-button'
import { ContextNote } from '@/components/hitl'
import {
  usePublicAddress,
  useRemoveTls,
  useSetPublicAddress,
  useSetTls,
  useTlsStatus,
} from '@/lib/queries'
import type { PublicAddressResponse, TlsStatus } from '@/lib/types'

const ERROR_BOX =
  'rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2 text-xs text-destructive'

function SectionHeading({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg border border-border bg-muted/40 text-muted-foreground">
        {icon}
      </span>
      <div className="min-w-0">
        <h2 className="font-display text-[15px] font-bold text-foreground">{title}</h2>
        <p className="mt-0.5 text-[13px] leading-snug text-muted-foreground">{children}</p>
      </div>
    </div>
  )
}

/** An https address the operator can pick; it only fills the box, the operator still saves. */
function SuggestionRow({ lead, url, onUse }: { lead: string; url: string; onUse: (url: string) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span>
        {lead}: <span className="font-mono text-foreground">{url}</span>
      </span>
      <Button size="sm" variant="outline" aria-label={`Use ${url}`} onClick={() => onUse(url)}>
        Use this
      </Button>
    </div>
  )
}

/** Addresses worth offering: what the server detected, plus the https page the operator is on now. */
function Suggestions({ data, onUse }: { data: PublicAddressResponse; onUse: (url: string) => void }) {
  const here = window.location.origin
  const offerHere = here.startsWith('https://') && here !== data.public_base_url
  return (
    <>
      {data.suggestions.map((s) => (
        <SuggestionRow
          key={`${s.source}:${s.url}`}
          lead={s.source === 'tailscale' ? 'Tailscale serve detected' : `${s.source} detected`}
          url={s.url}
          onUse={onUse}
        />
      ))}
      {offerHere && <SuggestionRow lead="Use the address you're on now" url={here} onUse={onUse} />}
    </>
  )
}

function AddressEditor({ data }: { data: PublicAddressResponse }) {
  const save = useSetPublicAddress()
  const [draft, setDraft] = useState<string | null>(null)
  const value = draft ?? data.public_base_url ?? ''

  return (
    <div className="space-y-2">
      <Input
        aria-label="Public address"
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(e) => setDraft(e.target.value)}
        placeholder="https://arc.example.com"
      />
      <Suggestions data={data} onUse={setDraft} />
      <div className="flex gap-2">
        <Button
          size="sm"
          disabled={save.isPending || value.trim() === ''}
          onClick={() => save.mutate(value.trim(), { onSuccess: () => setDraft(null) })}
        >
          {save.isPending ? 'Saving…' : 'Save address'}
        </Button>
        {data.public_base_url && (
          <Button
            size="sm"
            variant="outline"
            disabled={save.isPending}
            onClick={() => save.mutate(null, { onSuccess: () => setDraft(null) })}
          >
            Clear address
          </Button>
        )}
      </div>
      {save.isError && <p className={ERROR_BOX}>{save.error.message}</p>}
    </div>
  )
}

function PublicAddressSection({ editable }: { editable: boolean }) {
  const address = usePublicAddress()
  const data = address.data
  return (
    <section className="space-y-3">
      <SectionHeading icon={<Globe className="size-4" />} title="Public address">
        The address people use to open this dashboard from other computers. Sign-in with Google,
        Microsoft and Atlassian sends the browser back here.
      </SectionHeading>
      {address.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
      {address.isError && <p className={ERROR_BOX}>{address.error.message}</p>}
      {data && (
        <>
          {data.https_required && (
            <ContextNote tone="warning">https is required at this tier.</ContextNote>
          )}
          <p className="text-xs text-muted-foreground">
            Saved address:{' '}
            <span className="font-mono text-foreground">{data.public_base_url ?? 'not set'}</span>
          </p>
          <div className="space-y-1">
            <p className="text-xs text-muted-foreground">
              Sign-in return address (register this with each provider):
            </p>
            <div className="flex items-center gap-1">
              <Input readOnly aria-label="Sign-in return address" value={data.redirect_uri} />
              <CopyButton text={data.redirect_uri} />
            </div>
          </div>
          {editable && <AddressEditor key={data.public_base_url ?? ''} data={data} />}
        </>
      )}
    </section>
  )
}

function formatExpiry(notAfter: string): string {
  const date = new Date(notAfter)
  return Number.isNaN(date.getTime()) ? notAfter : date.toLocaleDateString()
}

function TlsDetails({ tls }: { tls: TlsStatus }) {
  return (
    <ul className="space-y-0.5 text-xs text-muted-foreground">
      <li>Certificate: {tls.configured ? 'saved' : 'none saved'}</li>
      <li>Serving https now: {tls.active ? 'yes' : 'no'}</li>
      {tls.subject && <li>Subject: {tls.subject}</li>}
      {tls.not_after && <li>Expires: {formatExpiry(tls.not_after)}</li>}
      {tls.dns_names.length > 0 && <li>Names: {tls.dns_names.join(', ')}</li>}
    </ul>
  )
}

function CertificateEditor({ tls }: { tls: TlsStatus }) {
  const save = useSetTls()
  const remove = useRemoveTls()
  const [cert, setCert] = useState('')
  const [key, setKey] = useState('')
  const clear = () => {
    setCert('')
    setKey('')
  }
  const error = save.error ?? remove.error

  return (
    <div className="space-y-2">
      <Textarea
        aria-label="Certificate (PEM)"
        spellCheck={false}
        rows={5}
        className="font-mono text-xs"
        value={cert}
        onChange={(e) => setCert(e.target.value)}
        placeholder="-----BEGIN CERTIFICATE-----"
      />
      <Textarea
        aria-label="Private key (PEM)"
        autoComplete="off"
        spellCheck={false}
        rows={5}
        className="font-mono text-xs"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        placeholder="-----BEGIN PRIVATE KEY-----"
      />
      <div className="flex gap-2">
        <Button
          size="sm"
          disabled={save.isPending || cert.trim() === '' || key.trim() === ''}
          onClick={() => save.mutate({ cert_pem: cert, key_pem: key }, { onSuccess: clear })}
        >
          {save.isPending ? 'Saving…' : 'Save certificate'}
        </Button>
        {tls.configured && !tls.required && (
          <Button size="sm" variant="outline" disabled={remove.isPending} onClick={() => remove.mutate()}>
            Remove certificate
          </Button>
        )}
      </div>
      {error && <p className={ERROR_BOX}>{error.message}</p>}
    </div>
  )
}

function CertificateSection({ editable }: { editable: boolean }) {
  const tlsQuery = useTlsStatus()
  const tls = tlsQuery.data
  const needsRestart = tls !== undefined && (tls.restart_required === true || tls.configured !== tls.active)
  return (
    <section className="space-y-3">
      <SectionHeading icon={<ShieldCheck className="size-4" />} title="HTTPS certificate">
        Use this when the dashboard is reached directly over your network or tailnet. If a reverse
        proxy or <span className="font-mono">tailscale serve</span> already provides https in front of
        the dashboard, you do not need a certificate here — just set the public address.
      </SectionHeading>
      {tlsQuery.isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
      {tlsQuery.isError && <p className={ERROR_BOX}>{tlsQuery.error.message}</p>}
      {tls && (
        <>
          {tls.required && <ContextNote tone="warning">A certificate is required at this tier.</ContextNote>}
          <TlsDetails tls={tls} />
          {needsRestart && (
            <ContextNote tone="info">
              Restart the dashboard to apply the certificate change. Use the Restart stack button at the
              top of this page.
            </ContextNote>
          )}
          {editable && <CertificateEditor tls={tls} />}
        </>
      )}
    </section>
  )
}

/**
 * Settings → Access: where this dashboard can be reached from, and the https
 * certificate it serves. Everything is done here in the browser.
 */
export function PublicAccessPanel({ editable }: { editable: boolean }) {
  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <PublicAddressSection editable={editable} />
      <CertificateSection editable={editable} />
    </div>
  )
}
