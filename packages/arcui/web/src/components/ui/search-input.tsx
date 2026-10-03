import * as React from 'react'
import { Search } from 'lucide-react'
import { FieldHelp } from '@/components/help'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

interface SearchInputProps extends React.ComponentProps<'input'> {
  /** Stable field-help ID; the "?" renders inline beside the box. */
  helpKey?: string
  helpRoute?: string
  /** Classes for the outer row (width caps). */
  wrapperClassName?: string
}

/**
 * Search box with its icon inside the field. The icon is centred against the
 * input box alone (never the row), so a help button beside the box cannot push
 * it out of line.
 */
export function SearchInput({ helpKey, helpRoute, wrapperClassName, className, ...props }: SearchInputProps) {
  return (
    <div className={cn('flex w-full max-w-sm items-center gap-1', wrapperClassName)}>
      <div className="relative min-w-0 flex-1">
        <Search
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-2.5 my-auto size-4 text-muted-foreground"
        />
        <Input className={cn('pl-8', className)} {...props} />
      </div>
      {helpKey && <FieldHelp helpKey={helpKey} route={helpRoute} />}
    </div>
  )
}
