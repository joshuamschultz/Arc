import { useState } from 'react'
import { KeyRound, Trash2, TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { QueryState, EmptyState } from '@/components/states'
import { useKeys, useSetKey, useClearKey } from '@/lib/queries'
import { ApiError } from '@/lib/api'
import type { KeyEntry } from '@/lib/types'
import { cn } from '@/lib/utils'

// The whole vocabulary of this panel. `/api/keys` returns presence and nothing
// else — no value, no prefix, no length, no hash (D-583) — so there is nothing
// here that could imply a key is displayable.
function PresenceChip({ entry }: { entry: KeyEntry }) {
  if (entry.present) {
    return (
      <span className="rounded-sm border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400">
        Set
      </span>
    )
  }
  return (
    <span
      className={cn(
        'rounded-sm border px-1.5 py-0.5 text-[11px] font-medium',
        entry.required
          ? 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400'
          : 'border-border bg-muted/40 text-muted-foreground',
      )}
    >
      Not set
    </span>
  )
}

function KeyRow({ entry, editable }: { entry: KeyEntry; editable: boolean }) {
  const setKey = useSetKey()
  const clearKey = useClearKey()
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)

  const busy = setKey.isPending || clearKey.isPending
  const fail = (fallback: string) => (e: Error) =>
    setError(e instanceof ApiError ? e.message : fallback)

  const save = () => {
    setError(null)
    setKey.mutate(
      { envVar: entry.env_var, value: draft },
      // Cleared on success so the credential lives no longer than the write.
      { onSuccess: () => setDraft(''), onError: fail('Could not save') },
    )
  }

  const clear = () => {
    setError(null)
    clearKey.mutate(entry.env_var, { onError: fail('Could not clear') })
  }

  return (
    <TableRow>
      <TableCell className="font-medium text-foreground">{entry.provider}</TableCell>
      <TableCell>
        <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
          {entry.env_var}
        </span>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {entry.required ? 'Required' : 'Optional'}
      </TableCell>
      <TableCell>
        <PresenceChip entry={entry} />
      </TableCell>
      {editable && (
        <TableCell>
          <div className="flex items-center gap-2">
            <Input
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && draft && save()}
              placeholder={entry.present ? 'Replace…' : 'Paste key…'}
              className="h-8 w-56"
              aria-label={`New value for ${entry.env_var}`}
            />
            <Button size="sm" disabled={busy || draft.length === 0} onClick={save}>
              Save
            </Button>
            {entry.present && (
              <Button
                variant="ghost"
                size="icon-sm"
                disabled={busy}
                onClick={clear}
                title={`Clear ${entry.env_var}`}
                className="text-destructive hover:text-destructive"
              >
                <Trash2 className="size-3.5" />
              </Button>
            )}
            {error && <span className="text-xs text-destructive">{error}</span>}
          </div>
        </TableCell>
      )}
    </TableRow>
  )
}

/**
 * Fleet-wide provider keys (`~/.arc/.env`). Required-and-missing keys sort to
 * the top and carry a banner: that is exactly the state in which a fresh
 * install does not work.
 */
export function KeysPanel({ editable }: { editable: boolean }) {
  const keys = useKeys()

  return (
    <QueryState
      query={keys}
      isEmpty={(data) => data.keys.length === 0}
      empty={
        <EmptyState
          icon={<KeyRound className="size-7" />}
          title="No providers"
          description="arcllm reported no providers, so there are no keys to set here."
        />
      }
    >
      {(data) => {
        const missing = data.keys.filter((k) => k.required && !k.present)
        // Required-and-missing first, then the rest in server order.
        const rows = [...missing, ...data.keys.filter((k) => !(k.required && !k.present))]
        return (
          <div className="space-y-4">
            {missing.length > 0 && (
              <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                <p>
                  {missing.length} required{' '}
                  {missing.length === 1 ? 'key is' : 'keys are'} not set:{' '}
                  <span className="font-mono">{missing.map((k) => k.env_var).join(', ')}</span>.
                  Agents using {missing.length === 1 ? 'that provider' : 'those providers'} will
                  not run until {missing.length === 1 ? 'it is' : 'they are'} set.
                </p>
              </div>
            )}
            {!editable && (
              <p className="text-xs italic text-muted-foreground/80">
                Enable operator mode to set or clear keys. A key is never displayed — this panel
                only ever shows whether one is set.
              </p>
            )}
            <div className="rounded-lg border border-border bg-card">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Provider</TableHead>
                    <TableHead>Environment variable</TableHead>
                    <TableHead>Required</TableHead>
                    <TableHead>Status</TableHead>
                    {editable && <TableHead>Set / clear</TableHead>}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((entry) => (
                    <KeyRow key={entry.env_var} entry={entry} editable={editable} />
                  ))}
                </TableBody>
              </Table>
            </div>
          </div>
        )
      }}
    </QueryState>
  )
}
