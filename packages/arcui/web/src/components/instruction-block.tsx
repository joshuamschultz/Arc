import { CopyButton } from '@/components/copy-button'

/**
 * Setup text an operator may need to run by hand. It wraps, it scrolls inside
 * its own box, and it can be copied whole — a command clipped mid-URL is the
 * same as no command at all.
 */
export function InstructionBlock({ title, text }: { title?: string; text: string }) {
  return (
    <div className="overflow-hidden rounded-md border border-border bg-background">
      <div className="flex items-center justify-between gap-2 border-b border-border bg-muted/40 px-2.5 py-1.5">
        <span className="min-w-0 text-[11px] font-medium text-foreground">
          {title ?? 'Type this on the computer running Arc'}
        </span>
        <CopyButton text={text} />
      </div>
      <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words px-2.5 py-2 font-mono text-[11px] leading-relaxed text-foreground">
        {text}
      </pre>
    </div>
  )
}
