import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { Lock } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { QueryState } from '@/components/states'
import { ApiError } from '@/lib/api'
import {
  useClassifierModels,
  useMemoryPromotion,
  useRunMemoryPromotion,
  useSaveMemoryPromotion,
  type MemoryPromotionRunResult,
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

// Classifier drop-in whose pinned models the dropdown lists.
const CLASSIFIER = 'jev'
const OTHER = '__other__'

/**
 * Model picker: the drop-in's pinned models plus "Other…" for free text, so a
 * pinned version the list does not know still saves. While the list is loading
 * (or unavailable) the field is plain text, so saving never depends on it.
 */
function ModelField({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string
  value: string
  onChange: (next: string) => void
  disabled: boolean
}) {
  const models = useClassifierModels(CLASSIFIER).data?.models
  const [other, setOther] = useState(false)
  const customId = useId()

  if (!models) {
    return (
      <Input
        id={id}
        aria-label="Classifier model"
        value={value}
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
      />
    )
  }

  const custom = other || !models.includes(value)
  return (
    <div className="space-y-2">
      <Select
        value={custom ? OTHER : value}
        onValueChange={(next) => (next === OTHER ? setOther(true) : (setOther(false), onChange(next)))}
        disabled={disabled}
      >
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {models.map((m) => (
            <SelectItem key={m} value={m}>
              {m}
            </SelectItem>
          ))}
          <SelectItem value={OTHER}>Other…</SelectItem>
        </SelectContent>
      </Select>
      {custom && (
        <Input
          id={customId}
          aria-label="Pinned model version"
          value={value}
          spellCheck={false}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        />
      )}
    </div>
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
          <ModelField id={ids.model} value={model} onChange={setModel} disabled={readOnly} />
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

const RUN_COUNTS: [keyof MemoryPromotionRunResult, string][] = [
  ['evaluated', 'Evaluated'],
  ['promoted', 'Promoted'],
  ['kept_private', 'Kept private'],
  ['blocked_secret', 'Blocked (secret)'],
  ['too_large', 'Too large'],
  ['deferred', 'Deferred'],
]

function RunResult({ result }: { result: MemoryPromotionRunResult }) {
  return (
    <div className="space-y-1 text-sm" aria-live="polite">
      <p className="text-foreground">
        Last run: <span className="font-medium">{result.status}</span>
      </p>
      <dl className="grid grid-cols-3 gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {RUN_COUNTS.map(([key, label]) => (
          <div key={key} className="flex justify-between gap-2">
            <dt>{label}</dt>
            <dd className="tabular-nums text-foreground">{result[key]}</dd>{' '}
          </div>
        ))}
      </dl>
    </div>
  )
}

/**
 * "Run now" (SPEC-083 COMP-029): promote this agent's existing memory once, now.
 * Enabled only for an operator when sharing is on in the SAVED settings and the
 * agent is not federal-locked; disabled while a run is in flight.
 */
function RunNow({ agentId, canRun }: { agentId: string; canRun: boolean }) {
  const run = useRunMemoryPromotion(agentId)
  const [error, setError] = useState<string | null>(null)

  const start = () => {
    setError(null)
    run.mutate(undefined, { onError: (e) => setError(errorText(e, 'Run failed')) })
  }

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <div className="flex items-center gap-3">
        <Button
          size="sm"
          variant="outline"
          onClick={start}
          disabled={!canRun || run.isPending}
          aria-busy={run.isPending}
        >
          Run now
        </Button>
        <span className="text-xs text-muted-foreground">
          {run.isPending
            ? 'Running…'
            : 'Sends existing memory once; the nightly run then sends only new or changed items.'}
        </span>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {run.data && !error && <RunResult result={run.data} />}
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
        {query.data && (
          <div className="flex items-center gap-2">
            <KeyBadge set={query.data.key_set} />
            <Link to="/settings" className="text-xs text-primary underline-offset-2 hover:underline">
              Set in Settings → Keys
            </Link>
          </div>
        )}
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
                  Turn on operator mode (top-right) to change these settings.
                </p>
              )}
              <SettingsForm
                key={`${settings.enabled}|${settings.confidence_threshold}|${settings.classifier_model}`}
                agentId={agentId}
                settings={settings}
                locked={locked}
                operatorMode={operatorMode}
              />
                            <RunNow agentId={agentId} canRun={operatorMode && settings.enabled && !locked} />
            </div>
          )
        }}
      </QueryState>
    </div>
  )
}
