import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BlueprintsSection } from '@/components/maintenance/blueprints-section'
import { renderWithClient, stubApi } from '@/components/maintenance/test-helpers'
import { describeCreates } from '@/lib/maintenance'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const SALES = {
  id: 'sales',
  name: 'sales',
  version: '1.0.0',
  tier: 'personal',
  description: 'Pipeline memory for a sales executive.',
  source: 'packaged',
  signed: true,
  creates: {
    persona: true,
    prompts: ['arcmemory/distill_fact', 'arcmemory/distill_day'],
    skills: ['deal-review'],
    capabilities: [],
    schedules: 1,
    modules: ['memory', 'tasks'],
  },
}

describe('describeCreates', () => {
  it('lists what the agent starts with in plain words', () => {
    expect(describeCreates(SALES.creates)).toBe(
      'a starting identity, 2 tuned prompts, 1 skill, 1 scheduled task; turns on memory, tasks',
    )
  })

  it('says so when the blueprint only carries settings', () => {
    expect(
      describeCreates({ persona: false, prompts: [], skills: [], capabilities: [], schedules: 0, modules: [] }),
    ).toBe('only its settings')
  })
})

describe('BlueprintsSection', () => {
  it('shows each blueprint and what it would create', async () => {
    stubApi({ 'GET /api/maintenance/blueprints': { blueprints: [SALES] } })
    renderWithClient(<BlueprintsSection editable />)

    expect(await screen.findByText('Pipeline memory for a sales executive.')).toBeTruthy()
    expect(screen.getByText(/a starting identity, 2 tuned prompts/)).toBeTruthy()
    expect(screen.getByText('Signed')).toBeTruthy()
  })

  it('creates an agent from a blueprint and links to it', async () => {
    const calls = stubApi({
      'GET /api/maintenance/blueprints': { blueprints: [SALES] },
      'POST /api/maintenance/blueprints/sales/create': {
        agent_id: 'closer',
        did: 'did:arc:x',
        team_registered: false,
        notice: 'Team messaging is offline, so this agent is not in team chat yet.',
        created: { persona: true, prompts: 2, capabilities: 0, skills: 1, schedules: 1 },
        warnings: 0,
      },
    })
    renderWithClient(<BlueprintsSection editable />)

    await userEvent.type(await screen.findByLabelText('Name for the new sales agent'), 'closer')
    await userEvent.click(screen.getByRole('button', { name: 'Create agent' }))

    const link = await screen.findByRole('link', { name: 'closer' })
    expect(link.getAttribute('href')).toBe('/agents/closer')
    expect(screen.getByText(/not in team chat yet/)).toBeTruthy()
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ agent_name: 'closer' })
  })

  it('keeps Create disabled until the agent has a name', async () => {
    stubApi({ 'GET /api/maintenance/blueprints': { blueprints: [SALES] } })
    renderWithClient(<BlueprintsSection editable />)

    const button = await screen.findByRole('button', { name: 'Create agent' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows the refusal in plain words when the name is taken', async () => {
    stubApi({
      'GET /api/maintenance/blueprints': { blueprints: [SALES] },
      'POST /api/maintenance/blueprints/sales/create': {
        status: 409,
        body: { error: 'An agent named closer already exists.' },
      },
    })
    renderWithClient(<BlueprintsSection editable />)

    await userEvent.type(await screen.findByLabelText('Name for the new sales agent'), 'closer')
    await userEvent.click(screen.getByRole('button', { name: 'Create agent' }))

    expect(await screen.findByRole('alert')).toHaveProperty(
      'textContent',
      'An agent named closer already exists.',
    )
  })

  it('offers no way to create when not in operator mode', async () => {
    stubApi({ 'GET /api/maintenance/blueprints': { blueprints: [SALES] } })
    renderWithClient(<BlueprintsSection editable={false} />)

    await screen.findByText('Pipeline memory for a sales executive.')
    expect(screen.queryByRole('button', { name: 'Create agent' })).toBeNull()
  })
})
