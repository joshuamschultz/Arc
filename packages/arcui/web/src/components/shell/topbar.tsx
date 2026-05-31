import { ArcLogo } from '@/components/arc-logo'

export function Topbar() {
  return (
    <header className="flex h-12 items-center gap-3 border-b border-border bg-background px-4">
      <div className="flex items-center gap-2">
        <ArcLogo />
        <span className="text-[15px] font-bold tracking-wide text-foreground">
          ARC
        </span>
        <span className="text-xs text-muted-foreground">Agent Platform</span>
      </div>
    </header>
  )
}
