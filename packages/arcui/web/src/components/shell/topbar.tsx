import { Moon, Sun } from 'lucide-react'
import { ArcLogo } from '@/components/arc-logo'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/hooks/use-theme'

export function Topbar() {
  const { dark, toggle } = useTheme()

  return (
    <header className="flex h-12 items-center gap-3 border-b border-border bg-background px-4">
      <div className="flex flex-1 items-center gap-2">
        <ArcLogo />
        <span className="text-[15px] font-bold tracking-wide text-foreground">
          ARC
        </span>
        <span className="text-xs text-muted-foreground">Agent Platform</span>
      </div>
      <Button
        variant="ghost"
        size="icon"
        onClick={toggle}
        aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
        className="h-8 w-8 text-muted-foreground hover:text-foreground"
      >
        {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
      </Button>
    </header>
  )
}
