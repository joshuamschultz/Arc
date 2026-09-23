import { useEffect } from 'react'
import { RouterProvider } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AuthGate } from '@/components/auth-gate'
import { router } from '@/app/router'
import { watchForStaleBuild } from '@/lib/stale-build'
import { createQueryClient } from '@/lib/query-client'

const queryClient = createQueryClient()

export default function App() {
  // A tab left open across a deploy runs code the server has deleted, and fails in
  // ways that read as unrelated app bugs. It cannot learn that from a cache header,
  // so it asks.
  useEffect(watchForStaleBuild, [])

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={200}>
        <AuthGate>
          <RouterProvider router={router} />
        </AuthGate>
      </TooltipProvider>
    </QueryClientProvider>
  )
}
