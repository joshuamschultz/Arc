import { useState } from 'react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useAddMcpServer, usePreviewMcpServer } from '@/lib/queries'
import { agentLabel, grantName } from '@/lib/agent-names'
import type { Agent, McpServerForm, McpToolChoice, McpToolView } from '@/lib/types'
import { cn } from '@/lib/utils'

type Transport = 'http' | 'stdio'

interface EnvRow {
  field: string
  variable: string
  value: string
}

const LABEL = 'text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground'
const BLANK_ROW: EnvRow = { field: '', variable: '', value: '' }

/**
 * Add your own MCP server (P12).
 *
 * The operator describes a server, asks it what it offers (nothing is written), chooses
 * the tools to expose and who may use them, and Arc generates, signs and installs a
 * connector for it. The safety rules (what may launch, where it may connect, which tools
 * it may expose) live in the generator on the server; this form only collects the
 * description and shows a refusal in words.
 *
 * A credential typed here lives in component state until the request lands or the dialog
 * closes. It is sent, never rendered back, never put in a URL, and the response has no
 * field able to hold one. A tool's description is the server's own text and is shown as
 * plain text, never interpreted. Classification is the operator's choice per tool and
 * defaults to the cautious one: whatever the server says about itself is not consulted.
 */
export function AddMcpServerDialog({
  agents,
  open,
  onOpenChange,
}: {
  /** The fleet, so the form can ask who may use this server. */
  agents: Agent[]
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const preview = usePreviewMcpServer()
  const add = useAddMcpServer()
  const [name, setName] = useState('')
  const [transport, setTransport] = useState<Transport>('http')
  const [url, setUrl] = useState('')
  const [authHeader, setAuthHeader] = useState('Authorization')
  const [credential, setCredential] = useState('')
  const [command, setCommand] = useState('')
  const [rows, setRows] = useState<EnvRow[]>([BLANK_ROW])
  const [discovered, setDiscovered] = useState<McpToolView[] | null>(null)
  const [tags, setTags] = useState<string[]>([])
  const [chosen, setChosen] = useState<Record<string, McpToolChoice>>({})
  const [granted, setGranted] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)

  const busy = preview.isPending || add.isPending
  const filledRows = rows.filter((r) => r.field.trim() && r.variable.trim())
  const reachable =
    name.trim().length > 0 && (transport === 'http' ? url.trim().length > 0 : command.trim().length > 0)
  const chosenCount = Object.keys(chosen).length

  const form = (): McpServerForm =>
    transport === 'http'
      ? {
          name: name.trim(),
          transport,
          url: url.trim(),
          auth_header: credential ? authHeader.trim() : '',
          secrets: credential ? { credential } : {},
        }
      : {
          name: name.trim(),
          transport,
          argv: command.trim().split(/\s+/),
          env_refs: Object.fromEntries(filledRows.map((r) => [r.field.trim(), r.variable.trim()])),
          secrets: Object.fromEntries(filledRows.map((r) => [r.field.trim(), r.value])),
        }

  const reset = () => {
    setName('')
    setUrl('')
    setCredential('')
    setCommand('')
    setRows([BLANK_ROW])
    setDiscovered(null)
    setChosen({})
    setGranted([])
    setError(null)
  }

  const handleOpenChange = (o: boolean) => {
    onOpenChange(o)
    if (!o) reset()
  }

  const discover = () => {
    setError(null)
    setChosen({})
    preview.mutate(form(), {
      onSuccess: (result) => {
        setDiscovered(result.tools)
        setTags(result.suggested_tags)
      },
      onError: (e) => setError(e.message),
    })
  }

  const toggleTool = (tool: McpToolView) =>
    setChosen((current) => {
      if (current[tool.name]) {
        return Object.fromEntries(Object.entries(current).filter(([k]) => k !== tool.name))
      }
      return {
        ...current,
        [tool.name]: {
          classification: 'state_modifying',
          capability_tags: tags,
          description: tool.description,
        },
      }
    })

  const classify = (tool: string, classification: McpToolChoice['classification']) =>
    setChosen((current) => ({ ...current, [tool]: { ...current[tool], classification } }))

  const toggleAgent = (agent: string) =>
    setGranted((current) =>
      current.includes(agent) ? current.filter((n) => n !== agent) : [...current, agent],
    )

  const submit = () => {
    setError(null)
    add.mutate(
      { ...form(), tools: chosen, agents: granted },
      {
        onSuccess: () => handleOpenChange(false),
        onError: (e) => setError(e.message),
      },
    )
  }

  const updateRow = (index: number, patch: Partial<EnvRow>) =>
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-lg"
      >
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">Add MCP server</SheetTitle>
          <SheetDescription>
            Connect a tool server of your own. Arc asks it what it offers, then signs a connector
            for the tools you choose.
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {error && (
            <div
              role="alert"
              className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive"
            >
              {error}
            </div>
          )}

          <div className="space-y-1.5">
            <label htmlFor="mcp-name" className={LABEL}>Name</label>
            <Input
              id="mcp-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="acme"
              autoComplete="off"
            />
            <p className="text-[11px] text-muted-foreground">
              Lowercase letters, digits and underscores. Its tools appear to agents as{' '}
              <span className="font-mono">{name.trim() || 'name'}__tool</span>.
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="mcp-transport" className={LABEL}>Transport</label>
            <select
              id="mcp-transport"
              value={transport}
              onChange={(e) => {
                setTransport(e.target.value as Transport)
                setDiscovered(null)
                setChosen({})
              }}
              className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              <option value="http">Hosted server (HTTPS)</option>
              <option value="stdio">Local command</option>
            </select>
          </div>

          {transport === 'http' ? (
            <>
              <div className="space-y-1.5">
                <label htmlFor="mcp-url" className={LABEL}>Server URL</label>
                <Input
                  id="mcp-url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://mcp.example.com/v1/mcp"
                  autoComplete="off"
                />
              </div>
              <div className="space-y-1.5">
                <label htmlFor="mcp-auth-header" className={LABEL}>Auth header</label>
                <Input
                  id="mcp-auth-header"
                  value={authHeader}
                  onChange={(e) => setAuthHeader(e.target.value)}
                  autoComplete="off"
                />
              </div>
              <div className="space-y-1.5">
                <label htmlFor="mcp-credential" className={LABEL}>Credential</label>
                <Input
                  id="mcp-credential"
                  type="password"
                  value={credential}
                  onChange={(e) => setCredential(e.target.value)}
                  autoComplete="off"
                />
                <p className="text-[11px] text-muted-foreground">
                  Stored once, never shown again. Leave blank if the server needs none.
                </p>
              </div>
            </>
          ) : (
            <>
              <div className="space-y-1.5">
                <label htmlFor="mcp-command" className={LABEL}>Command</label>
                <Input
                  id="mcp-command"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="/usr/local/bin/mcp-server --flag"
                  autoComplete="off"
                />
                <p className="text-[11px] text-muted-foreground">
                  A program and its arguments, separated by spaces. It runs directly, never through
                  a shell.
                </p>
              </div>
              {rows.map((row, index) => (
                <div key={index} className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <div className="space-y-1.5">
                    <label htmlFor={`mcp-env-field-${index}`} className={LABEL}>Credential name</label>
                    <Input
                      id={`mcp-env-field-${index}`}
                      value={row.field}
                      onChange={(e) => updateRow(index, { field: e.target.value })}
                      placeholder="api_key"
                      autoComplete="off"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor={`mcp-env-var-${index}`} className={LABEL}>Environment variable</label>
                    <Input
                      id={`mcp-env-var-${index}`}
                      value={row.variable}
                      onChange={(e) => updateRow(index, { variable: e.target.value })}
                      placeholder="API_KEY"
                      autoComplete="off"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label htmlFor={`mcp-env-value-${index}`} className={LABEL}>Credential value</label>
                    <Input
                      id={`mcp-env-value-${index}`}
                      type="password"
                      value={row.value}
                      onChange={(e) => updateRow(index, { value: e.target.value })}
                      autoComplete="off"
                    />
                  </div>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setRows((current) => [...current, BLANK_ROW])}
              >
                Add another credential
              </Button>
            </>
          )}

          <Button type="button" variant="outline" disabled={!reachable || busy} onClick={discover}>
            {preview.isPending ? 'Asking the server…' : 'Discover tools'}
          </Button>

          {discovered && (
            <fieldset className="space-y-2">
              <legend className={LABEL}>Tools to expose</legend>
              <p className="text-[11px] text-muted-foreground">
                Nothing is exposed until you tick it. Descriptions are the server&rsquo;s own words.
              </p>
              {discovered.map((tool) => (
                <div key={tool.name} className="rounded-md border border-border px-3 py-2">
                  <label className="flex items-start gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={!!chosen[tool.name]}
                      disabled={!tool.usable}
                      onChange={() => toggleTool(tool)}
                      className="mt-1"
                    />
                    <span>
                      <span className="font-mono">{tool.name}</span>
                      <span className="block text-[11px] text-muted-foreground">
                        {tool.usable ? tool.description : tool.reason}
                      </span>
                    </span>
                  </label>
                  {chosen[tool.name] && (
                    <select
                      aria-label={`${tool.name} access`}
                      value={chosen[tool.name].classification}
                      onChange={(e) =>
                        classify(tool.name, e.target.value as McpToolChoice['classification'])
                      }
                      className="mt-2 h-8 w-full rounded-md border border-input bg-transparent px-2 text-xs"
                    >
                      <option value="state_modifying">Changes things (asks first)</option>
                      <option value="read_only">Only reads</option>
                    </select>
                  )}
                </div>
              ))}
            </fieldset>
          )}

          {discovered && (
            <div className="space-y-1.5">
              <span className={LABEL}>Who can use it</span>
              <div className="flex flex-wrap gap-1.5">
                {agents.map((agent) => {
                  const key = grantName(agent)
                  const picked = granted.includes(key)
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => toggleAgent(key)}
                      className={cn(
                        'rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors',
                        picked
                          ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                          : 'border-dashed border-border text-muted-foreground hover:border-foreground/40',
                      )}
                    >
                      {agentLabel(agent)}
                    </button>
                  )
                })}
                {agents.length === 0 && (
                  <p className="text-[11px] text-muted-foreground">No agents on this deployment yet.</p>
                )}
              </div>
              <p className="text-[11px] text-muted-foreground">
                MCP servers expose tools only, so nothing from them is added to Knowledge.
              </p>
            </div>
          )}
        </div>
        <div className="flex justify-end gap-2 border-t border-border px-5 py-3">
          <Button variant="ghost" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={chosenCount === 0 || busy} onClick={submit}>
            {add.isPending ? 'Adding…' : 'Add server'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}
