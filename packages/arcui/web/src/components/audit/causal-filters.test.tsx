// SPEC-087: an app principal is a causal initiator ('app', arctrust.causal.Initiator);
// the ledger must let an operator filter to it.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CausalFilterBar } from './causal-filters'

afterEach(cleanup)

describe('CausalFilterBar initiators', () => {
  it('offers the app initiator and applies it', async () => {
    const onApply = vi.fn()
    render(<CausalFilterBar value={{}} onApply={onApply} />)
    const user = userEvent.setup()

    expect(screen.getByRole('option', { name: 'App' })).toBeTruthy()
    await user.selectOptions(screen.getByLabelText('Initiator'), 'app')
    await user.click(screen.getByRole('button', { name: /apply filters/i }))

    expect(onApply).toHaveBeenCalledWith(expect.objectContaining({ initiator: 'app' }))
  })
})
