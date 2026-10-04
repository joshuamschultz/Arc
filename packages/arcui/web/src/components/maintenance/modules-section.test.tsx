import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ModulesSection } from '@/components/maintenance/modules-section'
import { renderWithClient, stubApi } from '@/components/maintenance/test-helpers'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const MODULES = {
  agents: [
    { agent_id: 'olivia', name: 'Olivia' },
    { agent_id: 'mira', name: 'Mira' },
  ],
  modules: [
    {
      name: 'web',
      description: 'Web module: web search and extraction tools for ArcAgent.',
      installed: true,
      staged: { version: '2.0.0', issuer: 'did:arc:op', update_available: true },
      agents: { olivia: { enabled: true }, mira: { enabled: false } },
    },
    {
      name: 'browser',
      description: '',
      installed: false,
      staged: { version: '1.0.0', issuer: 'did:arc:op', update_available: true },
      agents: { olivia: { enabled: false }, mira: { enabled: false } },
    },
  ],
}

describe('ModulesSection', () => {
  it('lists every module with its state for each agent', async () => {
    stubApi({ 'GET /api/maintenance/modules': MODULES })
    renderWithClient(<ModulesSection editable />)

    expect(await screen.findByText('web')).toBeTruthy()
    expect(screen.getByText(/web search and extraction/i)).toBeTruthy()
    expect(screen.getByText('Not installed')).toBeTruthy()
    expect(screen.getAllByText('On').length).toBe(1)
    expect(screen.getAllByText('Off').length).toBe(3)
    expect(screen.getByText(/Signed bundle 2.0.0 is waiting \(newer than the one installed\)/)).toBeTruthy()
  })

  it('turns a module on for one agent and tells the person what happened', async () => {
    localStorage.setItem('arcui_operator_mode', '1') // the restart button is an operator control
    const calls = stubApi({
      'GET /api/maintenance/modules': MODULES,
      'POST /api/maintenance/modules/web/enable': {
        message: 'web is now on for mira. It takes effect when Arc restarts.',
        restart_needed: true,
      },
    })
    renderWithClient(<ModulesSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Turn on web for Mira' }))

    expect(await screen.findByText(/web is now on for mira/i)).toBeTruthy()
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ agent_id: 'mira' })
    // A change that waits for a restart offers the restart right there.
    expect(screen.getByRole('button', { name: /Restart Arc/ })).toBeTruthy()
  })

  it('turns a module off for one agent', async () => {
    const calls = stubApi({
      'GET /api/maintenance/modules': MODULES,
      'POST /api/maintenance/modules/web/disable': { message: 'web is now off for olivia.', restart_needed: false },
    })
    renderWithClient(<ModulesSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Turn off web for Olivia' }))

    expect(await screen.findByText('web is now off for olivia.')).toBeTruthy()
    expect(calls.find((c) => c.method === 'POST')?.path).toContain('/modules/web/disable')
  })

  it('upgrades from the staged signed bundle', async () => {
    const calls = stubApi({
      'GET /api/maintenance/modules': MODULES,
      'POST /api/maintenance/modules/web/install': { message: 'web 2.0.0 is installed for olivia.', restart_needed: true },
    })
    renderWithClient(<ModulesSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Update web for Olivia' }))

    expect(await screen.findByText(/web 2.0.0 is installed for olivia/)).toBeTruthy()
    expect(calls.find((c) => c.method === 'POST')?.path).toContain('/modules/web/install')
  })

  it('offers install, not turn-on, for a module that is not installed yet', async () => {
    stubApi({ 'GET /api/maintenance/modules': MODULES })
    renderWithClient(<ModulesSection editable />)

    expect(await screen.findByRole('button', { name: 'Install browser for Olivia' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Turn on browser for Olivia' })).toBeNull()
  })

  it('shows a refusal in plain words', async () => {
    stubApi({
      'GET /api/maintenance/modules': MODULES,
      'POST /api/maintenance/modules/browser/install': {
        status: 409,
        body: { error: 'This bundle is not signed by a key Arc trusts, so it was not installed.' },
      },
    })
    renderWithClient(<ModulesSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Install browser for Olivia' }))

    expect(await screen.findByText(/not signed by a key Arc trusts/)).toBeTruthy()
  })

  it('shows no change buttons when not in operator mode', async () => {
    stubApi({ 'GET /api/maintenance/modules': MODULES })
    renderWithClient(<ModulesSection editable={false} />)

    await screen.findByText('web')
    expect(screen.queryByRole('button')).toBeNull()
  })
})
