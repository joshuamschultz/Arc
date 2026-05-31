import { RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AuthGate } from '@/components/auth-gate'
import { useArcSocketConnect } from '@/hooks/use-arc-socket'
import { router } from '@/app/router'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, refetchOnWindowFocus: false, retry: 1 },
  },
})

function ConnectedApp() {
  useArcSocketConnect()
  return <RouterProvider router={router} />
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={200}>
        <AuthGate>
          <ConnectedApp />
        </AuthGate>
      </TooltipProvider>
    </QueryClientProvider>
  )
}
