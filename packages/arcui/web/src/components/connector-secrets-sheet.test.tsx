import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { ConnectorSecretsSheet } from '@/components/connector-secrets-sheet'
import type { CatalogBundle, ConnectorSecret } from '@/lib/types'

afterEach(cleanup)

const WARNING = "Blank uses gog's built-in sign-in client, whose Google sign-ins expire after about 7 days."

const field = (over: Partial<ConnectorSecret> & { name: string }): ConnectorSecret => ({
  prompt: over.name, sensitive: false, value: '', ...over,
})

const bundle = (secrets: ConnectorSecret[], remote = false): CatalogBundle => ({
  name: 'google_workspace', display_name: 'Google Workspace', version: '1.0.0',
  description: 'Gmail, Calendar and Drive', attachment: 'cli', tier_floor: 'personal',
  approval_default: 'ask', knowledge_mode: 'source', knowledge_reason: '',
  secrets,
  host_requires: [{ name: 'gog', instruction: 'install gog', satisfied: true, remote_login: remote }],
  tools: [], root: '/ext',
})

const google = () => bundle([
  field({ name: 'account', required: false }),
  field({ name: 'client', required: false, warning: WARNING }),
  field({ name: 'read_only', required: false, choices: ['yes', 'no'], default: 'yes' }),
], true)

function renderSheet(b: CatalogBundle) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConnectorSecretsSheet bundle={b} agents={[]} open onOpenChange={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function fillNameAndAccount() {
  await userEvent.type(screen.getAllByRole('textbox')[0], 'gmail-olivia')
  await userEvent.type(screen.getByLabelText('account'), 'olivia@example.com')
}

const connect = () => screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement

it('lets an optional field stay blank', async () => {
  renderSheet(bundle([field({ name: 'account', required: false }), field({ name: 'client', required: false })]))
  await fillNameAndAccount()
  expect(connect().disabled).toBe(false)
})

it('still asks for a field the server does not mark optional', async () => {
  renderSheet(bundle([field({ name: 'account', required: false }), field({ name: 'client' })]))
  await fillNameAndAccount()
  expect(connect().disabled).toBe(true)
})

it('renders a choice field as a select preselected to its default', () => {
  renderSheet(google())
  const select = screen.getByRole('combobox', { name: 'read_only' }) as HTMLSelectElement
  expect(select.value).toBe('yes')
  expect([...select.options].map((o) => o.value)).toEqual(['yes', 'no'])
})

it('warns under a blank field and hides the warning once filled', async () => {
  renderSheet(google())
  expect(screen.getByText(WARNING)).toBeTruthy()
  await userEvent.type(screen.getByLabelText('client'), 'my-client')
  expect(screen.queryByText(WARNING)).toBeNull()
})

it('needs the warned field on a remote-login bundle unless the operator accepts the warning', async () => {
  renderSheet(google())
  await fillNameAndAccount()
  expect(connect().disabled).toBe(true)
  await userEvent.click(screen.getByRole('checkbox', { name: /Leave client blank for now/ }))
  expect(connect().disabled).toBe(false)
})

it('accepts a filled warned field without the acknowledgement', async () => {
  renderSheet(google())
  await fillNameAndAccount()
  await userEvent.type(screen.getByLabelText('client'), 'my-client')
  expect(connect().disabled).toBe(false)
})

it('does not demand the warned field on an ordinary bundle', async () => {
  renderSheet(bundle([field({ name: 'account', required: false }), field({ name: 'client', required: false, warning: WARNING })]))
  await fillNameAndAccount()
  expect(connect().disabled).toBe(false)
})
