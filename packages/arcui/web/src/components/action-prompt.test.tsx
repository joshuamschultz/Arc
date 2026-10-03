// The action codes a refusal carries become plain sentences, with a button only
// where this dashboard has a path. No sentence names a terminal command.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ActionPrompt, RefusalNotice } from '@/components/action-prompt'
import { ApiError } from '@/lib/api'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function wrap(ui: React.ReactNode) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>)
}

describe('ActionPrompt', () => {
  it('sign_bundle is a plain sentence with no button and no command', () => {
    wrap(<ActionPrompt code="sign_bundle" />)
    expect(screen.getByText(/not signed with your key yet/)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByText(/\barc (connector|bundle|sign|add)\b/i)).toBeNull()
  })

  it('reseal_credentials opens the credential review panel from a button', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            state: 'needs_review',
            keys: [{ key: 'OLD_TOKEN', reason: 'undeclared', kept: false }],
            targets: [],
            affected_connections: ['gmail'],
          }),
        ),
      ),
    )
    wrap(<ActionPrompt code="reseal_credentials" />)

    await userEvent.click(await screen.findByRole('button', { name: 'Review credentials' }))

    expect(await screen.findByText('OLD_TOKEN')).toBeTruthy()
  })

  it('an unknown code renders nothing', () => {
    wrap(<ActionPrompt code="something_new" />)
    expect(screen.queryByText(/./)).toBeNull()
  })
})

describe('RefusalNotice', () => {
  it('shows the message and then the sentence for the code the server sent', () => {
    const error = new ApiError(400, 'Granting gmail needs the vault.', undefined, { action: 'sign_bundle' })
    wrap(<RefusalNotice error={error} />)
    expect(screen.getByText('Granting gmail needs the vault.')).toBeTruthy()
    expect(screen.getByText(/not signed with your key yet/)).toBeTruthy()
  })
})
