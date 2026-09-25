import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArcLogo } from '@/components/arc-logo'
import { FieldHelp } from '@/components/help'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { clearHostedClaim, hostedClaimSecret } from '@/lib/hosted-claim'
import { setToken } from '@/lib/auth'

async function request(
  controllers: Set<AbortController>, path: string, options: RequestInit = {}, timeoutMs = 8000,
) {
  const controller = new AbortController()
  controllers.add(controller)
  const deadline = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(path, { ...options, signal: controller.signal, cache: 'no-store' })
  } finally {
    window.clearTimeout(deadline)
    controllers.delete(controller)
  }
}

export function SetupPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState('Checking your setup…')
  const [password, setPassword] = useState('')
  const [repeat, setRepeat] = useState('')
  const [busy, setBusy] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const requests = useRef(new Set<AbortController>())

  useEffect(() => {
    let mounted = true
    const controllers = requests.current
    request(controllers, '/api/setup/status')
      .then(async (response) => {
        const data = await response.json()
        if (mounted) setStatus(data.status === 'awaiting_setup' ? 'Your server is ready for account setup.' :
          data.status === 'setup_complete' ? 'Your account is ready. Sign in to continue.' :
            'Secure setup is still being prepared. Return to your order page shortly.')
      })
      .catch(() => { if (mounted) setStatus('Secure setup is temporarily unavailable.') })
    return () => {
      mounted = false
      for (const controller of controllers) controller.abort()
    }
  }, [])

  async function submit() {
    const secret = hostedClaimSecret()
    if (!secret) {
      setError('Open the setup link from your verified order page again.')
      return
    }
    if (password.length < 12 || password !== repeat) {
      setError('Use at least 12 characters and enter the same password twice.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const response = await request(requests.current, '/api/setup/claim', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ customer_secret: secret, password }),
      }, 35000)
      const result = await response.json()
      if (result.status === 'claim_pending') {
        setPending(true)
        setStatus('Your account request is still finishing. Check setup status before trying again.')
        setError('The request may have succeeded. Wait briefly, then use the sign-in link if your account is ready.')
        return
      }
      if (!response.ok) {
        setError(result.error || 'Your claim could not be completed. Refresh your order page.')
        return
      }
      clearHostedClaim()
      const login = await request(requests.current, '/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: result.email, password }),
      })
      if (login.ok) {
        const session = await login.json()
        setToken(session.token)
        navigate('/home', { replace: true })
      } else {
        setStatus('Your account is ready. Sign in to continue.')
        setPassword('')
        setRepeat('')
      }
    } catch {
      setPending(true)
      setError('The request may still be finishing. Check setup status or sign in before retrying.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-6">
      <section className="w-full max-w-md rounded-xl border border-border bg-card p-8 shadow-lg">
        <div className="mb-6 flex items-center gap-2"><ArcLogo /><span className="text-lg font-bold">ARC</span></div>
        <h1 className="text-xl font-semibold">Create your account</h1>
        <p className="mt-2 text-sm text-muted-foreground">{status}<FieldHelp helpKey="setup.status" route="setup" /></p>
        <form className="mt-6 flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); void submit() }}>
          <label className="text-sm font-medium" htmlFor="setup-password">Password<FieldHelp helpKey="setup.password" route="setup" /></label>
          <Input id="setup-password" type="password" autoComplete="new-password" value={password}
            onChange={(event) => setPassword(event.target.value)} required minLength={12} />
          <label className="text-sm font-medium" htmlFor="setup-repeat">Repeat password<FieldHelp helpKey="setup.password_confirmation" route="setup" /></label>
          <Input id="setup-repeat" type="password" autoComplete="new-password" value={repeat}
            onChange={(event) => setRepeat(event.target.value)} required minLength={12} />
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          {pending && <Button type="button" variant="outline" onClick={() => {
            void request(requests.current, '/api/setup/status').then(async (response) => {
              const data = await response.json()
              if (data.status === 'setup_complete') {
                clearHostedClaim()
                setStatus('Your account is ready. Sign in to continue.')
                setError('')
                setPending(false)
              } else {
                setStatus('Your account request is still finishing. Please wait and check again.')
              }
            }).catch(() => setStatus('Setup status is temporarily unavailable.'))
          }}>Check setup status</Button>}
          <Button type="submit" disabled={busy || !password || !repeat}>
            {busy ? 'Creating account…' : 'Create account'}
          </Button>
          <FieldHelp helpKey="setup.create_account" route="setup" />
        </form>
        <a className="mt-5 inline-block text-sm underline" href="/">Already set up? Sign in</a>
        <FieldHelp helpKey="setup.sign_in" route="setup" />
      </section>
    </main>
  )
}
