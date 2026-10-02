/** Chip for a tool whose repeat call duplicates an effect it cannot undo. */
export function RepeatUnsafeChip() {
  return (
    <span className="inline-flex rounded-md border border-status-warning/30 bg-status-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-status-warning">
      Repeat is not safe
    </span>
  )
}
