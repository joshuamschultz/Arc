import { useAgentHandles } from '@/hooks/use-agent-handles'
import { cn } from '@/lib/utils'

/** A dropdown of the fleet's agents.
 *
 * A native select on purpose: the value is always a real handle or empty, never
 * free text. `inheritLabel` adds an empty-valued choice ("use the workflow
 * owner") for a node whose workflow already has a real owner; without it the
 * empty choice only reads as "choose" and callers keep the form unsaveable.
 */
export function AgentHandleSelect({
  value,
  onChange,
  label,
  inheritLabel,
  disabled,
  className,
}: {
  value: string
  onChange: (handle: string) => void
  label: string
  inheritLabel?: string
  disabled?: boolean
  className?: string
}) {
  const handles = useAgentHandles()
  // A value the roster no longer lists stays visible rather than silently
  // snapping to the first option.
  const options = value && !handles.includes(value) ? [value, ...handles] : handles
  return (
    <select
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={cn('h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm', className)}
    >
      <option value="">{inheritLabel ?? 'Choose an agent…'}</option>
      {options.map((handle) => (
        <option key={handle} value={handle}>
          {handle}
        </option>
      ))}
    </select>
  )
}
