import { Moon, Sun } from 'lucide-react'
import { ArcLogo } from '@/components/arc-logo'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/hooks/use-theme'

export function Topbar() {
  const { dark, toggle } = useTheme()

  return (
    <header className="sticky top-0 z-30 flex h-12 items-center gap-3 border-b border-border bg-background/80 px-4 backdrop-blur-md supports-[backdrop-filter]:bg-background/60">
      <div className="flex flex-1 items-center gap-2">
        <ArcLogo />
        <span className="text-[15px] font-bold tracking-tight text-foreground">
          ARC
        </span>
        <span className="hidden text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground sm:inline">
          Agent Platform
        </span>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={toggle}
        aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
        className="text-muted-foreground hover:text-foreground"
      >
        {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
      </Button>
    </header>
  )
}
