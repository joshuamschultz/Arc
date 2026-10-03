import type { ReactNode } from 'react'
import { ScreenHelp } from '@/components/help'
import { OperatorAvatarMenu } from '@/components/shell/operator-avatar-menu'

interface PageHeaderProps {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
}

/** Standard page heading used across every section. */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3 border-b border-border px-4 py-3 md:px-6 md:py-4">
      <div className="min-w-0 flex-1 basis-48">
        <h1 className="font-display text-[22px] font-extrabold leading-tight tracking-[-0.02em] text-foreground">
          {title}
        </h1>
        {description && (
          <p className="mt-1 text-[13px] leading-snug text-muted-foreground">
            {description}
          </p>
        )}
      </div>
      <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
        {actions}
        <ScreenHelp />
        <OperatorAvatarMenu />
      </div>
    </div>
  )
}
