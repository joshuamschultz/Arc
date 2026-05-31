import { cn } from '@/lib/utils'

/** The Arc mark — a sage arc over a dot. */
export function ArcLogo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={cn('size-7', className)}
      aria-hidden="true"
    >
      <path
        d="M4 17a8 8 0 0 1 16 0"
        stroke="var(--primary)"
        strokeWidth="2.4"
        strokeLinecap="round"
      />
      <circle cx="12" cy="19" r="2.2" fill="var(--primary)" />
    </svg>
  )
}
