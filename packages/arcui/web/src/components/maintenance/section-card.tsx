import { useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'

/** One titled block of the Maintenance tab. */
export function SectionCard({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: ReactNode
}) {
  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <div>
        <h2 className="font-display text-[15px] font-bold text-foreground">{title}</h2>
        <p className="mt-0.5 text-[13px] leading-snug text-muted-foreground">{description}</p>
      </div>
      {children}
    </section>
  )
}

/** A button that asks once more before it acts. The second step names what will happen. */
export function ConfirmButton({
  label,
  question,
  confirmLabel = 'Yes, do it',
  busy = false,
  disabled = false,
  destructive = false,
  onConfirm,
}: {
  label: string
  question: string
  confirmLabel?: string
  busy?: boolean
  disabled?: boolean
  destructive?: boolean
  onConfirm: () => void
}) {
  const [asking, setAsking] = useState(false)
  if (!asking) {
    return (
      <Button size="sm" variant="outline" disabled={disabled || busy} onClick={() => setAsking(true)}>
        {label}
      </Button>
    )
  }
  return (
    <span role="group" aria-label={`Confirm: ${label}`} className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-foreground">{question}</span>
      <Button
        size="sm"
        variant={destructive ? 'destructive' : 'default'}
        disabled={busy}
        onClick={() => {
          setAsking(false)
          onConfirm()
        }}
      >
        {confirmLabel}
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setAsking(false)}>
        Cancel
      </Button>
    </span>
  )
}
