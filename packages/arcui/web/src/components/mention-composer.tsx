import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Send } from 'lucide-react'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export interface MentionHandle {
  handle: string
  label: string
  color?: string
}

export interface CommandOption {
  /** The slash command's token, inserted as `/name `. */
  name: string
  /** Human-facing display name; falls back to `name`. */
  label: string
}

// The token the caret is currently editing — either an `@mention` anywhere in
// the message or a `/command` pinned to the very start. `at` is where the
// insertion begins; `query` is what has been typed after the trigger char.
interface ActiveToken {
  kind: 'mention' | 'command'
  at: number
  query: string
}

// One normalized suggestion the popup renders, whichever source produced it.
interface Suggestion {
  key: string
  /** Text spliced in at `at`, e.g. `@coder ` or `/help `. */
  insert: string
  at: number
  /** Primary label with its trigger char, e.g. `@coder` / `/help`. */
  primary: string
  /** Secondary muted label (the readable name), when it differs. */
  hint?: string
  color?: string
}

const MAX_SUGGESTIONS = 6

// Detect the ``@token`` the caret is currently editing. A mention only starts
// at the beginning of the input or after whitespace, and ends at the first
// whitespace — so ``a@b`` (an email-ish token) never opens the picker and a
// completed ``@intake `` is left alone.
function activeMention(value: string, caret: number): ActiveToken | null {
  const upto = value.slice(0, caret)
  const at = upto.lastIndexOf('@')
  if (at === -1) return null
  const before = at === 0 ? '' : upto[at - 1]
  if (before && !/\s/.test(before)) return null
  const query = upto.slice(at + 1)
  if (/\s/.test(query)) return null
  return { kind: 'mention', at, query }
}

// Detect the ``/command`` token — a slash is a command only as the first
// character of the message, and the token ends at the first whitespace, so a
// completed ``/help `` is left alone and a mid-message slash never triggers.
function activeCommand(value: string, caret: number): ActiveToken | null {
  const upto = value.slice(0, caret)
  if (!upto.startsWith('/')) return null
  const query = upto.slice(1)
  if (/\s/.test(query)) return null
  return { kind: 'command', at: 0, query }
}

/**
 * Autosizing composer with ``@name`` mention and ``/command`` autocomplete.
 * Enter sends; Shift+Enter inserts a newline.
 *
 * The inserted text is spliced verbatim into the value: an ``@handle`` carries
 * through arcteam's mention machinery (``apply_mentions``), and a ``/name``
 * lands as the operator's chosen command. Both share one popup and one set of
 * keyboard bindings — the only difference is which source (mentions vs
 * commands) the active token draws from. The component owns only the picker
 * UX; the parent owns the value and the send action.
 */
export function MentionComposer({
  value,
  onChange,
  onSubmit,
  handles,
  commands = [],
  placeholder,
  disabled = false,
  canSubmit = Boolean(value.trim()),
}: {
  value: string
  onChange: (next: string) => void
  onSubmit: () => void
  handles: MentionHandle[]
  commands?: CommandOption[]
  placeholder?: string
  disabled?: boolean
  canSubmit?: boolean
}) {
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const [token, setToken] = useState<ActiveToken | null>(null)
  const [active, setActive] = useState(0)

  const suggestions = useMemo<Suggestion[]>(() => {
    if (token === null) return []
    const q = token.query.toLowerCase()
    if (token.kind === 'command') {
      return commands
        .filter((c) => c.name.toLowerCase().includes(q) || c.label.toLowerCase().includes(q))
        .slice(0, MAX_SUGGESTIONS)
        .map((c) => ({
          key: `/${c.name}`,
          insert: `/${c.name} `,
          at: token.at,
          primary: `/${c.name}`,
          hint: c.label !== c.name ? c.label : undefined,
        }))
    }
    return handles
      .filter((h) => h.handle.toLowerCase().includes(q) || h.label.toLowerCase().includes(q))
      .slice(0, MAX_SUGGESTIONS)
      .map((h) => ({
        key: `@${h.handle}`,
        insert: `@${h.handle} `,
        at: token.at,
        primary: `@${h.handle}`,
        hint: h.label !== h.handle ? h.label : undefined,
        color: h.color,
      }))
  }, [token, handles, commands])

  const open = suggestions.length > 0

  const refresh = (next: string, caret: number) => {
    // A `/command` is start-anchored and thus more specific; check it first so a
    // leading slash never gets read as anything else.
    setToken(activeCommand(next, caret) ?? activeMention(next, caret))
    setActive(0)
  }

  const change = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    onChange(e.target.value)
    refresh(e.target.value, e.target.selectionStart ?? e.target.value.length)
  }

  const insert = (item: Suggestion) => {
    const el = inputRef.current
    const caret = el?.selectionStart ?? value.length
    const next = value.slice(0, item.at) + item.insert + value.slice(caret)
    onChange(next)
    setToken(null)
    setActive(0)
    const newCaret = item.at + item.insert.length
    requestAnimationFrame(() => {
      el?.focus()
      el?.setSelectionRange(newCaret, newCaret)
    })
  }

  const submit = () => {
    if (!canSubmit) return
    onSubmit()
  }

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (open) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActive((i) => (i + 1) % suggestions.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActive((i) => (i - 1 + suggestions.length) % suggestions.length)
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        insert(suggestions[active])
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setToken(null)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="relative flex flex-1 items-end gap-2">
      {open && (
        <ul
          role="listbox"
          className="absolute bottom-full left-0 z-20 mb-1.5 max-h-56 w-64 overflow-auto rounded-lg border border-border bg-popover p-1 shadow-lg"
        >
          {suggestions.map((s, i) => (
            <li key={s.key}>
              <button
                type="button"
                role="option"
                aria-selected={i === active}
                // mousedown fires before the input blurs, so the caret/value are
                // still intact when we splice the suggestion in.
                onMouseDown={(e) => {
                  e.preventDefault()
                  insert(s)
                }}
                onMouseEnter={() => setActive(i)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors',
                  i === active ? 'bg-primary/10 text-foreground' : 'text-muted-foreground hover:bg-muted/40',
                )}
              >
                <span
                  className="size-2 shrink-0 rounded-full"
                  style={{ background: s.color || 'var(--primary)' }}
                />
                <span className="font-medium text-foreground">{s.primary}</span>
                {s.hint && <span className="truncate text-xs text-muted-foreground">{s.hint}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      <Textarea
        ref={inputRef}
        value={value}
        onChange={change}
        onKeyDown={onKey}
        placeholder={placeholder}
        disabled={disabled}
      />
      <Button onClick={submit} disabled={disabled || !canSubmit} size="icon">
        <Send className="size-4" />
      </Button>
    </div>
  )
}
