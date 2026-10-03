import { useEffect, useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

import { MIN_PASSWORD_LENGTH, passwordProblem, postPublic, type SessionGrant } from '@/lib/auth-api'

function AuthCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <>
      <h1 className="text-base font-semibold text-foreground">{title}</h1>
      {children}
    </>
  )
}

function FormError({ message }: { message: string }) {
  if (!message) return null
  return (
    <p role="alert" className="mt-3 text-sm text-destructive">
      {message}
    </p>
  )
}

function PasswordFields({
  password,
  repeat,
  onPassword,
  onRepeat,
}: {
  password: string
  repeat: string
  onPassword: (v: string) => void
  onRepeat: (v: string) => void
}) {
  return (
    <>
      <Input
        type="password"
        autoComplete="new-password"
        placeholder="password"
        aria-label="Password"
        value={password}
        onChange={(e) => onPassword(e.target.value)}
      />
      <p className="text-xs text-muted-foreground">At least {MIN_PASSWORD_LENGTH} characters.</p>
      <Input
        type="password"
        autoComplete="new-password"
        placeholder="repeat password"
        aria-label="Repeat password"
        value={repeat}
        onChange={(e) => onRepeat(e.target.value)}
      />
    </>
  )
}

/** First run: no operator account exists yet, so the server-log code proves ownership. */
export function SetupForm({ onSignedIn }: { onSignedIn: (token: string) => void }) {
  const [code, setCode] = useState('')
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [repeat, setRepeat] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const problem = passwordProblem(password, repeat)
    if (problem) return setError(problem)
    setBusy(true)
    setError('')
    const result = await postPublic<SessionGrant>('/api/auth/setup', {
      setup_code: code.trim(),
      email: email.trim(),
      password,
      display_name: name.trim() || undefined,
    })
    setBusy(false)
    if (result.ok) onSignedIn(result.data.token)
    else setError(result.error)
  }

  return (
    <AuthCard title="Create the first operator account">
      <p className="mt-1 text-sm text-muted-foreground">
        When Arc started, it wrote a one-time setup code to its server log. Open the Arc server
        log and find the line that starts with &quot;ARC FIRST-RUN SETUP CODE&quot;. The code
        works once, and only until the first operator account exists. This keeps strangers from
        claiming your Arc.
      </p>
      <div className="mt-5 flex flex-col gap-2">
        <Input
          autoFocus
          autoComplete="off"
          placeholder="setup code"
          aria-label="Setup code"
          value={code}
          onChange={(e) => setCode(e.target.value)}
        />
        <Input
          type="email"
          autoComplete="username"
          placeholder="you@example.com"
          aria-label="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <Input
          autoComplete="name"
          placeholder="display name (optional)"
          aria-label="Display name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <PasswordFields
          password={password}
          repeat={repeat}
          onPassword={setPassword}
          onRepeat={setRepeat}
        />
        <Button onClick={submit} disabled={busy || !code.trim() || !email.trim() || !password}>
          {busy ? 'Creating…' : 'Create account'}
        </Button>
      </div>
      <FormError message={error} />
    </AuthCard>
  )
}

interface InviteInfo {
  email: string
  kind: 'invite' | 'reset'
  role: 'viewer' | 'operator'
}

/** Invite or password-reset link: confirm who it is for, then set a password. */
export function InviteScreen({
  token,
  onSignedIn,
  onBack,
}: {
  token: string
  onSignedIn: (token: string) => void
  onBack: () => void
}) {
  const [info, setInfo] = useState<InviteInfo | null>(null)
  const [expired, setExpired] = useState(false)
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [repeat, setRepeat] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    postPublic<InviteInfo>('/api/auth/invite/check', { token }).then((r) => {
      if (cancelled) return
      if (r.ok) setInfo(r.data)
      else setExpired(true)
    })
    return () => {
      cancelled = true
    }
  }, [token])

  const submit = async () => {
    const problem = passwordProblem(password, repeat)
    if (problem) return setError(problem)
    setBusy(true)
    setError('')
    const result = await postPublic<SessionGrant>('/api/auth/invite/accept', {
      token,
      password,
      display_name: name.trim() || undefined,
    })
    setBusy(false)
    if (result.ok) onSignedIn(result.data.token)
    else setError(result.error)
  }

  if (expired) {
    return (
      <AuthCard title="This link no longer works">
        <p className="mt-1 text-sm text-muted-foreground">
          The link has expired or was already used. Ask an operator for a new one.
        </p>
        <Button className="mt-5 w-full" onClick={onBack}>
          Back to sign in
        </Button>
      </AuthCard>
    )
  }
  if (!info) return <p className="text-sm text-muted-foreground">Checking your link…</p>

  const isReset = info.kind === 'reset'
  return (
    <AuthCard title="Set your password">
      <p className="mt-1 text-sm text-muted-foreground">
        {isReset
          ? `Reset the password for ${info.email}`
          : `You were invited as ${info.email} (${info.role})`}
      </p>
      <div className="mt-5 flex flex-col gap-2">
        {!isReset && (
          <Input
            autoComplete="name"
            placeholder="display name (optional)"
            aria-label="Display name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        )}
        <PasswordFields
          password={password}
          repeat={repeat}
          onPassword={setPassword}
          onRepeat={setRepeat}
        />
        <Button onClick={submit} disabled={busy || !password}>
          {busy ? 'Saving…' : 'Set password and sign in'}
        </Button>
      </div>
      <FormError message={error} />
    </AuthCard>
  )
}
