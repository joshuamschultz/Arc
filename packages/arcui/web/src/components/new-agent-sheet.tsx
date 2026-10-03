import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  AGENT_TIERS,
  DEFAULT_AGENT_MODEL,
  IMPORT_FILE_NAMES,
  agentNameProblem,
  useCreateAgent,
  type AgentTier,
  type NewAgentResult,
} from '@/lib/new-agent'

type Mode = 'create' | 'import'

const LABEL = 'text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground'

/** Read each picked file as text, keyed by its exact file name. */
async function readFiles(picked: Partial<Record<string, File>>): Promise<Record<string, string>> {
  const entries = await Promise.all(
    Object.entries(picked).map(async ([name, file]) => [name, await file!.text()] as const),
  )
  return Object.fromEntries(entries)
}

function FilePicker({
  name,
  required,
  onPick,
}: {
  name: string
  required: boolean
  onPick: (file: File | undefined) => void
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={`file-${name}`} className={LABEL}>
        {name} {required ? '(required)' : '(optional)'}
      </label>
      <Input
        id={`file-${name}`}
        type="file"
        accept=".md,text/markdown,text/plain"
        onChange={(e) => onPick(e.target.files?.[0])}
      />
    </div>
  )
}

/** Operator form: make a new agent from scratch, or bring in one from files. */
export function NewAgentSheet({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const navigate = useNavigate()
  const [mode, setMode] = useState<Mode>('create')
  const [name, setName] = useState('')
  const [model, setModel] = useState(DEFAULT_AGENT_MODEL)
  const [tier, setTier] = useState<AgentTier>('personal')
  const [picked, setPicked] = useState<Partial<Record<string, File>>>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const create = useCreateAgent(mode)

  const nameProblem = name ? agentNameProblem(name) : ''
  const ready = !agentNameProblem(name) && (mode === 'create' || Boolean(picked['identity.md']))

  const finish = (result: NewAgentResult) => {
    if (result.notice) {
      setNotice(result.notice)
      return
    }
    onOpenChange(false)
    navigate(`/agents/${result.agent_id}`)
  }

  const submit = async () => {
    setError('')
    const base = { name, model: model.trim() || undefined, tier }
    try {
      const body = mode === 'create' ? base : { ...base, files: await readFiles(picked) }
      create.mutate(body, { onSuccess: finish, onError: (e) => setError(e.message) })
    } catch {
      setError('Could not read one of the files.')
    }
  }

  if (notice) {
    return (
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-md">
          <SheetHeader className="border-b border-border px-5 py-4">
            <SheetTitle className="text-sm">Agent created</SheetTitle>
            <SheetDescription>{notice}</SheetDescription>
          </SheetHeader>
          <div className="p-5">
            <Button
              className="w-full"
              onClick={() => {
                onOpenChange(false)
                navigate(`/agents/${create.data?.agent_id ?? ''}`)
              }}
            >
              Open the agent
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    )
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-md">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">New agent</SheetTitle>
          <SheetDescription>Make a new agent, or bring one in from its files.</SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          <div className="grid grid-cols-2 gap-2" role="group" aria-label="How to add the agent">
            {(['create', 'import'] as const).map((m) => (
              <Button
                key={m}
                variant={mode === m ? 'default' : 'outline'}
                aria-pressed={mode === m}
                onClick={() => setMode(m)}
              >
                {m === 'create' ? 'Create' : 'Import'}
              </Button>
            ))}
          </div>
          {error && (
            <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          <div className="space-y-1.5">
            <label htmlFor="new-agent-name" className={LABEL}>Name</label>
            <Input id="new-agent-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="research-helper" />
            <p className={nameProblem ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
              {nameProblem || 'Lowercase letters, digits, - or _. 2 to 40 characters.'}
            </p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="new-agent-model" className={LABEL}>Model</label>
            <Input id="new-agent-model" value={model} onChange={(e) => setModel(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <label className={LABEL}>Tier</label>
            <Select value={tier} onValueChange={(v) => setTier(v as AgentTier)}>
              <SelectTrigger className="w-full" aria-label="Tier"><SelectValue /></SelectTrigger>
              <SelectContent>
                {AGENT_TIERS.map((t) => (
                  <SelectItem key={t} value={t}>{t}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {mode === 'import' &&
            IMPORT_FILE_NAMES.map((fileName) => (
              <FilePicker
                key={fileName}
                name={fileName}
                required={fileName === 'identity.md'}
                onPick={(file) => setPicked((p) => ({ ...p, [fileName]: file }))}
              />
            ))}
          <Button className="w-full" disabled={create.isPending || !ready} onClick={submit}>
            {create.isPending ? 'Working…' : mode === 'create' ? 'Create agent' : 'Import agent'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}
