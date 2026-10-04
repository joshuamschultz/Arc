import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, FileCode2, PackagePlus, ShieldCheck, Upload } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError } from '@/lib/api'
import {
  useApproveConnectorBundle,
  useConnectorBundles,
  useDiscardConnectorBundle,
  useRemoveConnectorBundle,
  useStageLocalConnectorBundle,
  useUploadConnectorBundle,
} from '@/lib/queries'
import type {
  BundleReview,
  BundleReviewUpdate,
  InstalledBundle,
  StagedBundle,
} from '@/lib/types'

const MAX_BYTES = 50 * 1024 * 1024
const ACCEPTED = /\.(zip|tar\.gz|tgz)$/i
const LABEL = 'text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground'

/** Reasons where the only fix is to start over with a fresh upload. */
const UPLOAD_AGAIN = new Set(['changed_after_review', 'expired'])

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function refusalText(error: Error): string {
  const reason = error instanceof ApiError ? error.body?.reason : undefined
  if (typeof reason === 'string' && UPLOAD_AGAIN.has(reason)) {
    return `${error.message} Upload the package again.`
  }
  return error.message
}

function usedByNames(error: Error | null): string[] {
  if (!(error instanceof ApiError)) return []
  const used = error.body?.used_by
  return Array.isArray(used) ? used.map(String) : []
}

function ErrorLine({ error }: { error: Error | null }) {
  if (!error) return null
  return (
    <p role="alert" className="text-xs text-destructive">
      {refusalText(error)}
    </p>
  )
}

/** The button on the Connections page. Hidden when the viewer cannot write. */
export function ConnectorBundleUploadButton({ operatorMode }: { operatorMode: boolean }) {
  const [open, setOpen] = useState(false)
  if (!operatorMode) return null
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <PackagePlus /> Add connector package
      </Button>
      {open && <ConnectorBundleSheet open onOpenChange={setOpen} />}
    </>
  )
}

export function ConnectorBundleSheet({
  open,
  onOpenChange,
  localName,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  localName?: string
}) {
  const [staged, setStaged] = useState<StagedBundle | null>(null)
  const [installed, setInstalled] = useState<InstalledBundle | null>(null)
  const upload = useUploadConnectorBundle()
  const stageLocal = useStageLocalConnectorBundle()
  const discard = useDiscardConnectorBundle()
  const autoStaged = useRef(false)
  const { mutate: stageLocalMutate } = stageLocal

  useEffect(() => {
    if (!open || !localName || autoStaged.current) return
    autoStaged.current = true
    stageLocalMutate(localName, { onSuccess: setStaged })
  }, [open, localName, stageLocalMutate])

  const close = () => {
    if (staged && !installed) discard.mutate(staged.staging_id)
    onOpenChange(false)
  }

  const startError = upload.error ?? stageLocal.error

  return (
    <Sheet open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>Add connector package</SheetTitle>
          <SheetDescription>
            A connector package adds a new service you can connect to. Read what it does before you
            approve it.
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-4 px-4 pb-6">
          {installed ? (
            <DoneView installed={installed} onClose={close} />
          ) : staged ? (
            <ReviewView
              staged={staged}
              onCancel={close}
              onInstalled={setInstalled}
            />
          ) : (
            <>
              <DropZone
                busy={upload.isPending || stageLocal.isPending}
                onFile={(file) => upload.mutate(file, { onSuccess: setStaged })}
              />
              <ErrorLine error={startError} />
              <InstalledList
                busy={stageLocal.isPending}
                onReviewLocal={(name) => stageLocal.mutate(name, { onSuccess: setStaged })}
              />
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

function DropZone({ busy, onFile }: { busy: boolean; onFile: (file: File) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [over, setOver] = useState(false)

  const take = (file: File | undefined) => {
    if (!file) return
    if (!ACCEPTED.test(file.name)) {
      setProblem('Choose a .zip, .tar.gz or .tgz file.')
      return
    }
    if (file.size > MAX_BYTES) {
      setProblem('That file is too big. The limit is 50 MB.')
      return
    }
    setProblem(null)
    onFile(file)
  }

  return (
    <div className="space-y-2">
      <div
        role="button"
        tabIndex={0}
        aria-label="Drop a connector package here or choose a file"
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click()
        }}
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setOver(false)
          take(e.dataTransfer.files[0])
        }}
        className={`flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed p-8 text-center text-sm ${
          over ? 'border-primary bg-primary/5' : 'border-border'
        }`}
      >
        <Upload className="size-5 text-muted-foreground" />
        <p>{busy ? 'Unpacking and checking the package...' : 'Drop a connector package here, or click to choose one.'}</p>
        <p className="text-xs text-muted-foreground">.zip, .tar.gz or .tgz, up to 50 MB</p>
        <input
          ref={inputRef}
          type="file"
          accept=".zip,.tar.gz,.tgz"
          aria-label="Connector package file"
          className="hidden"
          onChange={(e) => {
            take(e.target.files?.[0])
            e.target.value = ''
          }}
        />
      </div>
      {problem && (
        <p role="alert" className="text-xs text-destructive">
          {problem}
        </p>
      )}
    </div>
  )
}

function InstalledList({
  busy,
  onReviewLocal,
}: {
  busy: boolean
  onReviewLocal: (name: string) => void
}) {
  const bundles = useConnectorBundles()
  const remove = useRemoveConnectorBundle()
  const [removing, setRemoving] = useState<string | null>(null)
  const unsigned = bundles.data?.unsigned_local ?? []
  const installed = bundles.data?.installed ?? []
  const blockedBy = usedByNames(remove.error)

  return (
    <div className="space-y-4">
      {unsigned.length > 0 && (
        <section className="space-y-2">
          <h3 className={LABEL}>Waiting for your review</h3>
          <p className="text-xs text-muted-foreground">
            These packages are already on this computer. Arc will not run them until you review and
            sign them.
          </p>
          <ul className="space-y-2">
            {unsigned.map((item) => (
              <li
                key={item.name}
                className="flex items-center justify-between gap-3 rounded-md border border-border p-3 text-sm"
              >
                <span className="min-w-0">
                  <span className="block font-medium">{item.name}</span>
                  <span className="block text-xs text-muted-foreground">{item.reason}</span>
                </span>
                <Button size="sm" variant="outline" disabled={busy} onClick={() => onReviewLocal(item.name)}>
                  Review and sign
                </Button>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="space-y-2">
        <h3 className={LABEL}>Installed packages</h3>
        {installed.length === 0 ? (
          <p className="text-xs text-muted-foreground">No connector packages added yet.</p>
        ) : (
          <ul className="space-y-2">
            {installed.map((item) => (
              <li key={item.name} className="space-y-1 rounded-md border border-border p-3 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-medium">
                    {item.display_name} <span className="text-muted-foreground">v{item.version}</span>
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={remove.isPending}
                    onClick={() => {
                      setRemoving(item.name)
                      remove.mutate(item.name)
                    }}
                  >
                    Remove
                  </Button>
                </div>
                <p className="break-all text-xs text-muted-foreground">Signed by {item.signer_did}</p>
                {item.used_by.length > 0 && (
                  <p className="text-xs text-muted-foreground">Used by {item.used_by.join(', ')}</p>
                )}
                {removing === item.name && remove.error && (
                  <p role="alert" className="text-xs text-destructive">
                    {remove.error.message}
                    {blockedBy.length > 0 && ` In use by: ${blockedBy.join(', ')}.`}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function ReviewView({
  staged,
  onCancel,
  onInstalled,
}: {
  staged: StagedBundle
  onCancel: () => void
  onInstalled: (installed: InstalledBundle) => void
}) {
  const { review } = staged
  const approve = useApproveConnectorBundle()
  const [typed, setTyped] = useState('')
  const ready = !review.confirm_required || typed === review.name
  const minutes = Math.max(1, Math.round(staged.expires_in / 60))

  return (
    <div className="space-y-4">
      <ReviewHeader review={review} />
      {review.flags.length > 0 && (
        <ul aria-label="Warnings" className="space-y-1.5">
          {review.flags.map((flag) => (
            <li
              key={flag}
              className="flex items-start gap-2 rounded-md border border-status-warning/30 bg-status-warning/5 p-2 text-xs"
            >
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-status-warning" />
              <span>{flag}</span>
            </li>
          ))}
        </ul>
      )}
      {review.update && <DiffView update={review.update} version={review.version} />}
      <ReviewDetails review={review} />
      <div className="space-y-2 border-t border-border pt-4">
        {review.confirm_required && (
          <label className="block space-y-1 text-xs">
            <span>Type {review.name} to approve</span>
            <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </label>
        )}
        <ErrorLine error={approve.error} />
        <div className="flex items-center gap-2">
          <Button
            disabled={!ready || approve.isPending}
            onClick={() =>
              approve.mutate(
                { stagingId: staged.staging_id, confirmName: typed || review.name },
                { onSuccess: (res) => onInstalled(res.installed) },
              )
            }
          >
            Approve and sign
          </Button>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <span className="ml-auto text-xs text-muted-foreground">
            This review expires in about {minutes} {minutes === 1 ? 'minute' : 'minutes'}.
          </span>
        </div>
      </div>
    </div>
  )
}

function ReviewHeader({ review }: { review: BundleReview }) {
  const { publisher } = review
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-base font-semibold">{review.display_name}</h3>
        <span className="text-xs text-muted-foreground">
          {review.name} v{review.version}
        </span>
        {publisher.status === 'verified' ? (
          <Badge variant="secondary">
            <ShieldCheck /> Verified publisher
          </Badge>
        ) : (
          <Badge variant="outline">
            {publisher.status === 'unsigned' ? 'Not signed' : 'Unknown publisher'}
          </Badge>
        )}
      </div>
      {publisher.signer_did && (
        <p className="break-all text-xs text-muted-foreground">Signer: {publisher.signer_did}</p>
      )}
      <p className="text-sm">{review.description}</p>
    </div>
  )
}

function DiffView({ update, version }: { update: BundleReviewUpdate; version: string }) {
  const rows: [string, string[]][] = [
    ['Tools added', update.tools_added],
    ['Tools removed', update.tools_removed],
    ['Tools changed', update.tools_changed],
    ['New secrets', update.new_secrets],
    ['New network hosts', update.new_egress],
  ]
  return (
    <section className="space-y-2 rounded-md border border-border p-3">
      <h4 className="text-sm font-medium">
        Update from {update.installed_version} to {version}
      </h4>
      {rows
        .filter(([, items]) => items.length > 0)
        .map(([label, items]) => (
          <p key={label} className="text-xs">
            <span className="text-muted-foreground">{label}: </span>
            {items.join(', ')}
          </p>
        ))}
      <p className="text-xs text-muted-foreground">
        Changed tools stop working until you approve them on the connection card.
      </p>
    </section>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h4 className={LABEL}>{title}</h4>
      {children}
    </section>
  )
}

function ReviewDetails({ review }: { review: BundleReview }) {
  return (
    <div className="space-y-4">
      <Section title="Tools">
        {review.tools.length === 0 ? (
          <p className="text-xs text-muted-foreground">This package adds no tools.</p>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-muted-foreground">
                <th className="py-1 pr-2 font-medium">Name</th>
                <th className="py-1 pr-2 font-medium">Type</th>
                <th className="py-1 font-medium">Tags</th>
              </tr>
            </thead>
            <tbody>
              {review.tools.map((tool) => (
                <tr key={tool.name} className="border-t border-border align-top">
                  <td className="py-1.5 pr-2">
                    <span className="font-medium">{tool.name}</span>
                    <span className="block text-muted-foreground">{tool.description}</span>
                  </td>
                  <td className="py-1.5 pr-2">{tool.classification}</td>
                  <td className="space-x-1 py-1.5">
                    {tool.capability_tags.map((tag) => (
                      <Badge key={tag} variant="outline">
                        {tag}
                      </Badge>
                    ))}
                    {tool.network && <Badge variant="secondary">network</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </Section>
      {review.secrets.length > 0 && (
        <Section title="Secrets it asks for">
          <ul className="space-y-1 text-xs">
            {review.secrets.map((secret) => (
              <li key={secret.name}>
                <span className="font-medium">{secret.name}</span>
                {secret.required && <span className="text-status-warning"> (required)</span>}
                <span className="text-muted-foreground"> {secret.prompt}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <ListSection title="Programs it runs on this computer" items={review.host_programs} />
      <ListSection title="Network hosts it talks to" items={review.egress_hosts} />
      <ListSection title="Skills" items={review.skills} />
      <Section title="Security level">
        <p className="text-xs">{review.tier_floor}</p>
      </Section>
      <Section title="Files">
        <ul className="space-y-0.5 text-xs">
          {review.files.map((file) => (
            <li key={file.path} className="flex items-center gap-2">
              <span className="break-all font-mono">{file.path}</span>
              <span className="text-muted-foreground">{humanSize(file.size)}</span>
              {file.executes && (
                <Badge variant="outline" className="text-status-warning">
                  <FileCode2 /> runs code
                </Badge>
              )}
            </li>
          ))}
        </ul>
      </Section>
    </div>
  )
}

function ListSection({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <Section title={title}>
      <p className="text-xs">{items.join(', ')}</p>
    </Section>
  )
}

function DoneView({ installed, onClose }: { installed: InstalledBundle; onClose: () => void }) {
  return (
    <div className="space-y-3">
      <p className="text-sm">
        Installed. Find {installed.display_name} under Available and click Connect.
      </p>
      <Button onClick={onClose}>Close</Button>
    </div>
  )
}
