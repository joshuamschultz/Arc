import { useEffect } from 'react'
import { RouterProvider } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { TooltipProvider } from '@/components/ui/tooltip'
import { router } from '@/app/router'
import { watchForNewBuild } from '@/lib/stale-build'
import { createQueryClient } from '@/lib/query-client'

const queryClient = createQueryClient()

export default function App() {
  // A tab left open across a deploy runs code the server has deleted, and fails in
  // ways that read as unrelated app bugs. It cannot learn that from a cache header,
  // so it asks, and the shell shows a reload banner when the answer is yes.
  useEffect(watchForNewBuild, [])

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={200}>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>
  )
}
