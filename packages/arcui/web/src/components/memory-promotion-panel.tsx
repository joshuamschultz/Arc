import { useId, useState } from 'react'
import { Lock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { QueryState } from '@/components/states'
import { ApiError } from '@/lib/api'
import {
  useMemoryPromotion,
  useSaveJevKey,
  useSaveMemoryPromotion,
  type MemoryPromotionSettings,
} from '@/lib/queries'
import { cn } from '@/lib/utils'

const LABEL = 'mb-1 block text-[11px] uppercase tracking-[0.06em] text-muted-foreground'

const errorText = (e: Error, fallback: string) => (e instanceof ApiError ? e.message : fallback)

function Toggle({
  on,
  disabled,
  onChange,
}: {
  on: boolean
  disabled: boolean
  onChange: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label="Enable memory sharing"
      disabled={disabled}
      onClick={() => onChange(!on)}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-border transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 disabled:cursor-not-allowed disabled:opacity-50',
        on ? 'bg-emerald-500' : 'bg-muted',
      )}
    >
      <span
        className={cn(
          'inline-block size-4 rounded-full bg-background shadow-sm transition-transform',
          on ? 'translate-x-4' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}

function KeyBadge({ set }: { set: boolean }) {
  return (
    <span
      className={cn(
        'rounded-sm border px-1.5 py-0.5 text-[11px] font-medium',
        set
          ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
          : 'border-border bg-muted/40 text-muted-foreground',
      )}
    >
      {set ? 'Key set' : 'Key not set'}
    </span>
  )
}

/**
 * The three settings. Draft state is seeded from the server once per saved
 * version (the parent keys this component on those values), so a refusal keeps
 * the operator's edits and a successful save re-seeds from the server's reply.
 */
function SettingsForm({
  agentId,
  settings,
  locked,
  operatorMode,
}: {
  agentId: string
  settings: MemoryPromotionSettings
  locked: boolean
  operatorMode: boolean
}) {
  const save = useSaveMemoryPromotion(agentId)
  const [enabled, setEnabled] = useState(settings.enabled)
  const [threshold, setThreshold] = useState(String(settings.confidence_threshold))
  const [model, setModel] = useState(settings.classifier_model)
  const [error, setError] = useState<string | null>(null)
  const ids = { threshold: useId(), model: useId() }
  const readOnly = locked || !operatorMode || save.isPending

  const submit = () => {
    setError(null)
    save.mutate(
      { enabled, confidence_threshold: Number(threshold), classifier_model: model.trim() },
      { onError: (e) => setError(errorText(e, 'Could not save settings')) },
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Toggle on={enabled} disabled={readOnly} onChange={setEnabled} />
        <span className="text-sm text-foreground">Share company knowledge with the fleet</span>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor={ids.threshold} className={LABEL}>
            Confidence threshold
          </label>
          <Input
            id={ids.threshold}
            type="number"
            inputMode="decimal"
            step="0.01"
            min="0.9"
            max="1"
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
            disabled={readOnly}
          />
        </div>
        <div>
          <label htmlFor={ids.model} className={LABEL}>
            Classifier model
          </label>
          <Input
            id={ids.model}
            value={model}
            spellCheck={false}
            onChange={(e) => setModel(e.target.value)}
            disabled={readOnly}
          />
        </div>
      </div>
      {operatorMode && (
        <Button size="sm" onClick={submit} disabled={readOnly}>
          {save.isPending ? 'Saving…' : 'Save settings'}
        </Button>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  )
}

/** Write-only key field: the value goes to `/api/keys` and is dropped on success. */
function JevKeyForm({ agentId, disabled }: { agentId: string; disabled: boolean }) {
  const saveKey = useSaveJevKey(agentId)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const id = useId()
  const busy = disabled || saveKey.isPending

  const submit = () => {
    setError(null)
    saveKey.mutate(draft, {
      onSuccess: () => setDraft(''),
      onError: (e) => setError(errorText(e, 'Could not save key')),
    })
  }

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <label htmlFor={id} className={LABEL}>
        Jev API key
      </label>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Paste key…"
          className="h-8"
          disabled={busy}
        />
        <Button size="sm" onClick={submit} disabled={busy || draft.length === 0}>
          Save key
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  )
}

/**
 * "Memory sharing" — per-agent private -> shared promotion (SPEC-083 COMP-028).
 * Viewers see the settings read-only; federal-tier agents are locked with the
 * reason shown, because promotion sends memory to a third-party classifier.
 */
export function MemoryPromotionPanel({
  agentId,
  operatorMode,
}: {
  agentId: string
  operatorMode: boolean
}) {
  const query = useMemoryPromotion(agentId)

  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold tracking-tight text-foreground">Memory sharing</h3>
        {query.data && <KeyBadge set={query.data.key_set} />}
      </div>
      <QueryState query={query}>
        {(settings) => {
          const locked = settings.federal_locked
          return (
            <div className="space-y-3">
              {locked ? (
                <p className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-2 text-sm text-muted-foreground">
                  <Lock className="mt-0.5 size-4 shrink-0" />
                  Locked: this agent runs at the federal tier, where memory is never sent to a
                  third-party classifier.
                </p>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Promotes this agent's company knowledge to the shared store when the classifier
                  is confident. Personal items stay private.
                </p>
              )}
              {!operatorMode && (
                <p className="text-xs italic text-muted-foreground/80">
                  Turn on operator mode (top-right) to change these settings or set the key.
                </p>
              )}
              <SettingsForm
                key={`${settings.enabled}|${settings.confidence_threshold}|${settings.classifier_model}`}
                agentId={agentId}
                settings={settings}
                locked={locked}
                operatorMode={operatorMode}
              />
              {operatorMode && <JevKeyForm agentId={agentId} disabled={locked} />}
            </div>
          )
        }}
      </QueryState>
    </div>
  )
}
