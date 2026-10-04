import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TeamSection } from '@/components/maintenance/team-section'
import { renderWithClient, stubApi } from '@/components/maintenance/test-helpers'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const member = (over: Record<string, unknown>) => ({
  did: 'did:arc:local:user/dana',
  handle: 'dana',
  name: 'Dana Reyes',
  type: 'user',
  roles: ['reviewer'],
  status: 'active',
  harness: 'arcagent',
  created: '2026-10-01T00:00:00Z',
  protected: false,
  ...over,
})

const MEMBERS = {
  members: [
    member({ did: 'did:arc:local:agent/intake', handle: 'intake', name: 'Intake', type: 'agent', roles: [] }),
    member({ did: 'did:arc:local:user/operator', handle: 'operator', name: 'Operator', roles: [], protected: true }),
    member({}),
  ],
}

const ROSTER = {
  agents: [
    { agent_id: 'intake', name: 'intake', did: 'did:arc:local:agent/intake', harness: 'arcagent' },
    { agent_id: 'mira', name: 'mira', display_name: 'Mira', did: 'did:arc:local:agent/mira', harness: 'arcagent' },
  ],
}

describe('TeamSection', () => {
  it('lists agents and people with their status', async () => {
    stubApi({ 'GET /api/maintenance/team/members': MEMBERS, 'GET /api/team/roster': ROSTER })
    renderWithClient(<TeamSection editable />)

    expect(await screen.findByText('Dana Reyes')).toBeTruthy()
    expect(screen.getByText(/@dana · Person · reviewer/)).toBeTruthy()
    expect(screen.getByText(/@intake · Agent/)).toBeTruthy()
    expect(screen.getAllByText('Active').length).toBe(3)
  })

  it('adds a person', async () => {
    const calls = stubApi({
      'GET /api/maintenance/team/members': MEMBERS,
      'GET /api/team/roster': ROSTER,
      'POST /api/maintenance/team/members': member({}),
    })
    renderWithClient(<TeamSection editable />)

    await userEvent.type(await screen.findByLabelText('Name'), 'Eli Park')
    await userEvent.type(screen.getByLabelText('Handle'), 'eli')
    await userEvent.type(screen.getByLabelText('Roles'), 'reviewer, approver')
    await userEvent.click(screen.getByRole('button', { name: 'Add person' }))

    await vi.waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
        handle: 'eli',
        name: 'Eli Park',
        roles: ['reviewer', 'approver'],
      }),
    )
  })

  it('shows a taken handle in plain words', async () => {
    stubApi({
      'GET /api/maintenance/team/members': MEMBERS,
      'GET /api/team/roster': ROSTER,
      'POST /api/maintenance/team/members': { status: 409, body: { error: 'The handle eli is already taken.' } },
    })
    renderWithClient(<TeamSection editable />)

    await userEvent.type(await screen.findByLabelText('Name'), 'Eli')
    await userEvent.type(screen.getByLabelText('Handle'), 'eli')
    await userEvent.click(screen.getByRole('button', { name: 'Add person' }))

    expect(await screen.findByText('The handle eli is already taken.')).toBeTruthy()
  })

  it('switches a member off', async () => {
    const calls = stubApi({
      'GET /api/maintenance/team/members': MEMBERS,
      'GET /api/team/roster': ROSTER,
      'POST /api/maintenance/team/members/status': member({ status: 'suspended' }),
    })
    renderWithClient(<TeamSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Switch off Dana Reyes' }))

    await vi.waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
        did: 'did:arc:local:user/dana',
        status: 'suspended',
      }),
    )
  })

  it('removes a member only after a second confirm', async () => {
    const calls = stubApi({
      'GET /api/maintenance/team/members': MEMBERS,
      'GET /api/team/roster': ROSTER,
      'POST /api/maintenance/team/members/status': member({ status: 'revoked' }),
    })
    renderWithClient(<TeamSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Remove Dana Reyes' }))
    expect(calls.filter((c) => c.method === 'POST')).toEqual([])
    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))

    await vi.waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
        did: 'did:arc:local:user/dana',
        status: 'revoked',
      }),
    )
  })

  it('never offers to switch off or remove the operator', async () => {
    stubApi({ 'GET /api/maintenance/team/members': MEMBERS, 'GET /api/team/roster': ROSTER })
    renderWithClient(<TeamSection editable />)

    await screen.findByText('Operator')
    expect(screen.queryByRole('button', { name: 'Switch off Operator' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Remove Operator' })).toBeNull()
  })

  it('offers to put an agent back on the team when it fell off the list', async () => {
    const calls = stubApi({
      'GET /api/maintenance/team/members': MEMBERS,
      'GET /api/team/roster': ROSTER,
      'POST /api/agents/mira/register': { agent_id: 'mira', did: 'did:arc:local:agent/mira', team_registered: true },
    })
    renderWithClient(<TeamSection editable />)

    await userEvent.click(await screen.findByRole('button', { name: 'Add Mira to the team' }))

    await vi.waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.path).toContain('/api/agents/mira/register'),
    )
    // The agent that is already on the team gets no such button.
    expect(screen.queryByRole('button', { name: /Add intake to the team/ })).toBeNull()
  })

  it('is read-only outside operator mode', async () => {
    stubApi({ 'GET /api/maintenance/team/members': MEMBERS, 'GET /api/team/roster': ROSTER })
    renderWithClient(<TeamSection editable={false} />)

    await screen.findByText('Dana Reyes')
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByLabelText('Handle')).toBeNull()
  })

  it('says when team messaging is offline', async () => {
    stubApi({
      'GET /api/maintenance/team/members': {
        status: 503,
        body: { error: 'Team messaging is offline right now, so the member list is not available.' },
      },
    })
    renderWithClient(<TeamSection editable />)

    expect(await screen.findByText(/Team messaging is offline/)).toBeTruthy()
  })
})
