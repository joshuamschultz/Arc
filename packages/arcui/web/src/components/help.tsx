import { CircleHelp } from 'lucide-react'
import { Popover as PopoverPrimitive } from 'radix-ui'
import { useLocation } from 'react-router-dom'
import { fieldHelp, screenHelp } from '@/lib/help'

/** Contextual help for the current route, including detail tabs. */
export function ScreenHelp() {
  const { pathname } = useLocation()
  const help = screenHelp(pathname)
  if (!help) return null

  return (
    <PopoverPrimitive.Root>
      <PopoverPrimitive.Trigger asChild>
        <button
          type="button"
          aria-label={`Help for ${help.title}`}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
        >
          <CircleHelp className="size-4" /> Help
        </button>
      </PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align="end"
          sideOffset={8}
          collisionPadding={12}
          aria-label={`${help.title} help`}
          className="z-50 max-h-[min(70vh,32rem)] w-[min(24rem,calc(100vw-1.5rem))] overflow-auto rounded-lg border border-border bg-card p-4 text-sm text-foreground shadow-lg outline-none"
        >
          <h2 className="font-display text-base font-bold">{help.title} help</h2>
          <p className="mt-2 text-muted-foreground">{help.summary}</p>
          <ol className="mt-3 list-decimal space-y-1 pl-5">
            {help.steps.map((step) => <li key={step}>{step}</li>)}
          </ol>
          {help.troubleshooting.length > 0 && (
            <div className="mt-4 border-t border-border pt-3">
              <h3 className="text-xs font-semibold uppercase tracking-wide">Troubleshooting</h3>
              {help.troubleshooting.map(({ symptom, action }) => (
                <p key={symptom} className="mt-2 text-xs"><strong>{symptom}:</strong> {action}</p>
              ))}
            </div>
          )}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  )
}

/** Guidance attached to a stable field ID from the help inventory. */
export function FieldHelp({ helpKey, route }: { helpKey: string; route?: string }) {
  const help = fieldHelp(helpKey, route)
  if (!help) return null
  return (
    <PopoverPrimitive.Root>
      <PopoverPrimitive.Trigger asChild>
        <button
          type="button"
          aria-label={`Help for ${help.label}`}
          onClick={(event) => event.stopPropagation()}
          className="ml-1 inline-flex align-middle text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
        >
          <CircleHelp className="size-3.5" />
        </button>
      </PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          sideOffset={6}
          collisionPadding={12}
          className="z-50 max-h-[50vh] w-[min(20rem,calc(100vw-1.5rem))] overflow-auto rounded-md border border-border bg-card p-3 text-xs leading-relaxed text-foreground shadow-lg outline-none"
        >
          <h3 className="font-semibold">{help.label}</h3>
          <p className="mt-1">{help.description}</p>
          {help.example && <p className="mt-2 text-muted-foreground">Example: {help.example}</p>}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  )
}
