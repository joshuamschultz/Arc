import { expect, it } from 'vitest'
import { filterCapabilities, type CapabilityFilters } from './capability-filters'

const tools = [
  { name: 'read', agents: ['mc', 'ada'], source: 'builtin' },
  { name: 'bash', agents: ['ada'], source: 'builtin' },
  { name: 'crm', agents: ['mc'], source: 'extension' },
  { name: 'legacy', agents: ['mc'], source: '' },
]
const skills = [
  { name: 's1', agent_id: 'mc', source: 'agent' },
  { name: 's2', agent_id: 'ada', source: 'module' },
]
const none: CapabilityFilters = { agent: 'all', type: 'all', sources: new Set() }
const names = (rows: { name: string }[]) => rows.map((r) => r.name)

it('shows everything with no filters', () => {
  const out = filterCapabilities(tools, skills, none)
  expect(out.tools).toHaveLength(4)
  expect(out.skills).toHaveLength(2)
})

it('filters by agent for tools and skills', () => {
  const out = filterCapabilities(tools, skills, { ...none, agent: 'ada' })
  expect(names(out.tools)).toEqual(['read', 'bash'])
  expect(names(out.skills)).toEqual(['s2'])
})

it('type filter zeroes the other kind so counts follow', () => {
  expect(filterCapabilities(tools, skills, { ...none, type: 'skills' }).tools).toHaveLength(0)
  expect(filterCapabilities(tools, skills, { ...none, type: 'tools' }).skills).toHaveLength(0)
})

it('selected source chips keep only those sources', () => {
  const out = filterCapabilities(tools, skills, { ...none, sources: new Set(['builtin']) })
  expect(names(out.tools)).toEqual(['read', 'bash'])
  expect(out.skills).toHaveLength(0)
})

it('an empty source reads as agent', () => {
  const out = filterCapabilities(tools, skills, { ...none, sources: new Set(['agent']) })
  expect(names(out.tools)).toEqual(['legacy'])
  expect(names(out.skills)).toEqual(['s1'])
})

it('combines agent, type and source', () => {
  const out = filterCapabilities(tools, skills, { agent: 'mc', type: 'tools', sources: new Set(['extension']) })
  expect(names(out.tools)).toEqual(['crm'])
  expect(out.skills).toHaveLength(0)
})
