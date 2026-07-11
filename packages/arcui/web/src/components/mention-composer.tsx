import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Send } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export interface MentionHandle {
  handle: string
  label: string
  color?: string
}

interface ActiveMention {
  at: number
  query: string
}

const MAX_SUGGESTIONS = 6

// Detect the ``@token`` the caret is currently editing. A mention only starts
// at the beginning of the input or after whitespace, and ends at the first
// whitespace — so ``a@b`` (an email-ish token) never opens the picker and a
// completed ``@intake `` is left alone.
function activeMention(value: string, caret: number): ActiveMention | null {
  const upto = value.slice(0, caret)
  const at = upto.lastIndexOf('@')
  if (at === -1) return null
  const before = at === 0 ? '' : upto[at - 1]
  if (before && !/\s/.test(before)) return null
  const query = upto.slice(at + 1)
  if (/\s/.test(query)) return null
  return { at, query }
}

/**
 * Single-line composer with ``@name`` autocomplete against the team roster.
 *
 * The mention text is inserted verbatim into the value, so a posted message
 * carries ``@handle`` through arcteam's existing mention machinery
 * (``apply_mentions`` records it and raises the recipient's attention flag).
 * The component owns only the picker UX — the parent owns the value and the
 * send action.
 */
export function MentionComposer({
  value,
  onChange,
  onSubmit,
  handles,
  placeholder,
  disabled = false,
}: {
  value: string
  onChange: (next: string) => void
  onSubmit: () => void
  handles: MentionHandle[]
  placeholder?: string
  disabled?: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [mention, setMention] = useState<ActiveMention | null>(null)
  const [active, setActive] = useState(0)

  const matches = useMemo(() => {
    if (mention === null) return []
    const q = mention.query.toLowerCase()
    return handles
      .filter((h) => h.handle.toLowerCase().includes(q) || h.label.toLowerCase().includes(q))
      .slice(0, MAX_SUGGESTIONS)
  }, [mention, handles])

  const open = matches.length > 0

  const refresh = (next: string, caret: number) => {
    const found = activeMention(next, caret)
    setMention(found)
    setActive(0)
  }

  const change = (e: React.ChangeEvent<HTMLInputElement>) => {
    onChange(e.target.value)
    refresh(e.target.value, e.target.selectionStart ?? e.target.value.length)
  }

  const insert = (handle: string) => {
    const el = inputRef.current
    const caret = el?.selectionStart ?? value.length
    const found = activeMention(value, caret)
    const at = found ? found.at : value.length
    const inserted = `@${handle} `
    const next = value.slice(0, at) + inserted + value.slice(caret)
    onChange(next)
    setMention(null)
    setActive(0)
    const newCaret = at + inserted.length
    requestAnimationFrame(() => {
      el?.focus()
      el?.setSelectionRange(newCaret, newCaret)
    })
  }

  const submit = () => {
    if (!value.trim()) return
    onSubmit()
  }

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (open) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActive((i) => (i + 1) % matches.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActive((i) => (i - 1 + matches.length) % matches.length)
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        insert(matches[active].handle)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setMention(null)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="relative flex flex-1 items-center gap-2">
      {open && (
        <ul
          role="listbox"
          className="absolute bottom-full left-0 z-20 mb-1 max-h-56 w-64 overflow-auto rounded-md border border-border bg-popover p-1 shadow-md"
        >
          {matches.map((m, i) => (
            <li key={m.handle}>
              <button
                type="button"
                role="option"
                aria-selected={i === active}
                // mousedown fires before the input blurs, so the caret/value are
                // still intact when we splice the mention in.
                onMouseDown={(e) => {
                  e.preventDefault()
                  insert(m.handle)
                }}
                onMouseEnter={() => setActive(i)}
                className={cn(
                  'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm',
                  i === active ? 'bg-primary/15 text-foreground' : 'text-muted-foreground',
                )}
              >
                <span
                  className="size-2 shrink-0 rounded-full"
                  style={{ background: m.color || 'var(--primary)' }}
                />
                <span className="font-medium text-foreground">@{m.handle}</span>
                {m.label !== m.handle && <span className="truncate text-xs">{m.label}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      <Input
        ref={inputRef}
        value={value}
        onChange={change}
        onKeyDown={onKey}
        placeholder={placeholder}
        disabled={disabled}
      />
      <Button onClick={submit} disabled={disabled || !value.trim()} size="icon">
        <Send className="size-4" />
      </Button>
    </div>
  )
}
