import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { apiPost } from '@/lib/api'
import { cn } from '@/lib/utils'

/** One of the three parts that must all be up: gateway, mic client, speech engine. */
export type VoicePart = { up: boolean; heartbeat_at?: number | null; reason?: string }

export type VoiceLiveStatus = {
  listening: boolean
  state: 'offline' | 'paused' | 'listening' | 'heard' | 'speaking' | string
  client_connected: boolean
  reason: string
  mode?: string
  mic?: string
  wake_words: string[]
  wake_mode: string
  wake_loaded?: boolean
  last_wake_at?: number | null
  adapter: VoicePart
  client: VoicePart
  engine: VoicePart & { stt?: string | null; tts?: string | null }
  test_heard?: string | null
}

const STATE_LABEL: Record<string, string> = {
  offline: 'Offline',
  paused: 'Paused',
  listening: 'Listening',
  heard: 'Heard a wake word',
  speaking: 'Speaking',
}

const STATE_DOT: Record<string, string> = {
  offline: 'bg-status-idle',
  paused: 'bg-severity-medium',
  listening: 'bg-status-online',
  heard: 'bg-status-online',
  speaking: 'bg-status-online',
}

const CUSTOM_WORD_NOTE =
  'Any word works instantly. It is matched on a local transcript of what the mic box hears, ' +
  'not a trained wake-word model, so it uses a little CPU while people talk and a similar-sounding ' +
  'word can wake it.'

function ageLabel(heartbeat?: number | null): string {
  if (!heartbeat) return 'no heartbeat'
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - heartbeat))
  return seconds < 2 ? 'just now' : `${seconds}s ago`
}

function PartRow({ label, part }: { label: string; part: VoicePart }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right">
        <span className={cn('font-medium', part.up ? 'text-status-online' : 'text-destructive')}>
          {part.up ? 'Up' : 'Down'}
        </span>
        <span className="ml-2 text-xs text-muted-foreground">{ageLabel(part.heartbeat_at)}</span>
        {!part.up && part.reason && <p className="text-xs text-muted-foreground">{part.reason}</p>}
      </dd>
    </div>
  )
}

function parseWords(text: string): string[] {
  return text
    .split(',')
    .map((word) => word.trim().toLowerCase())
    .filter(Boolean)
}

function errorText(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback
}

/** Live voice status, the Listening ON/OFF switch, and the typed wake word. */
export function VoiceLive({
  agentId,
  operatorMode,
  live,
  onChanged,
}: {
  agentId: string
  operatorMode: boolean
  live: VoiceLiveStatus
  onChanged: () => void
}) {
  const [wakeText, setWakeText] = useState(live.wake_words.join(', '))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [wakeNote, setWakeNote] = useState<string | null>(null)
  const base = `/api/agents/${agentId}/voice`

  const act = async (run: () => Promise<void>, fallback: string) => {
    setBusy(true)
    setError(null)
    try {
      await run()
      onChanged()
    } catch (e) {
      setError(errorText(e, fallback))
    } finally {
      setBusy(false)
    }
  }

  const toggle = () =>
    act(async () => {
      await apiPost(`${base}/listening`, { on: !live.listening })
    }, 'Could not change listening')

  const saveWake = () =>
    act(async () => {
      const res = await apiPost<{ note?: string }>(`${base}/wake`, { words: parseWords(wakeText) })
      setWakeNote(res.note ?? null)
    }, 'Could not save the wake word')

  const startTest = () =>
    act(async () => {
      await apiPost(`${base}/test`, {})
    }, 'Could not start the test')

  const state = live.state
  return (
    <div className="mb-4 space-y-3 rounded-md border border-border bg-muted/30 p-3 text-sm">
      <div className="flex items-center justify-between gap-3">
        <span className="inline-flex items-center gap-2" role="status" aria-label="Voice status">
          <span className={cn('size-2.5 rounded-full', STATE_DOT[state] ?? 'bg-status-idle')} />
          <span className="font-medium">{STATE_LABEL[state] ?? state}</span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={live.listening}
          aria-label="Listening"
          disabled={!operatorMode || busy}
          onClick={() => void toggle()}
          className={cn(
            'relative inline-flex h-6 w-11 items-center rounded-full border border-border transition-colors disabled:opacity-50',
            live.listening ? 'bg-status-online' : 'bg-muted',
          )}
        >
          <span
            className={cn(
              'inline-block size-4 rounded-full bg-background transition-transform',
              live.listening ? 'translate-x-6' : 'translate-x-1',
            )}
          />
        </button>
      </div>
      {live.reason && <p className="text-muted-foreground">{live.reason}</p>}
      {!live.client.up && (
        <p className="text-xs text-muted-foreground">
          The Arc mic app is not running. Start it on the computer that has the microphone, and set
          it to start by itself so it comes back after a restart.
        </p>
      )}
      <dl className="space-y-2">
        <PartRow label="Gateway adapter" part={live.adapter} />
        <PartRow label="Mic app" part={live.client} />
        <PartRow label="Speech engine" part={live.engine} />
      </dl>
      {live.client_connected && (
        <p className="text-xs text-muted-foreground">
          Mic: {live.mic || 'unknown'} · mode {live.mode || 'unknown'}
          {live.wake_loaded ? '' : ' · wake word not loaded'}
        </p>
      )}
      <div>
        <label
          htmlFor="voice-wake-word"
          className="mb-1 block text-[11px] uppercase tracking-[0.06em] text-muted-foreground"
        >
          Wake word
        </label>
        <div className="flex gap-2">
          <Input
            id="voice-wake-word"
            value={wakeText}
            onChange={(e) => setWakeText(e.target.value)}
            placeholder="olivia, hey olivia"
            disabled={!operatorMode || busy}
          />
          <Button onClick={() => void saveWake()} disabled={!operatorMode || busy || !wakeText.trim()}>
            Save
          </Button>
          <Button variant="outline" onClick={() => void startTest()} disabled={!operatorMode || busy}>
            Say it now
          </Button>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">{wakeNote ?? CUSTOM_WORD_NOTE}</p>
        {live.test_heard != null && (
          <p className="mt-1 text-xs">
            Heard: <span className="font-mono">{live.test_heard || '…'}</span>
          </p>
        )}
      </div>
      {error && <p className="text-destructive">{error}</p>}
    </div>
  )
}
