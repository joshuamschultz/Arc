import type { ReactNode } from 'react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { JsonBlock } from '@/components/json-block'

interface EventDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  /** Raw payload rendered as JSON below any custom children. */
  payload?: unknown
  children?: ReactNode
}

/** Right-side slide-in showing an event/record's detail + raw JSON. */
export function EventDrawer({
  open,
  onOpenChange,
  title,
  description,
  payload,
  children,
}: EventDrawerProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl"
      >
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">{title}</SheetTitle>
          {description && <SheetDescription>{description}</SheetDescription>}
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {children}
          {payload !== undefined && (
            <div>
              <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Raw
              </div>
              <JsonBlock value={payload} />
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
