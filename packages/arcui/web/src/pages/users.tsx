import { useState, type ReactNode } from 'react'
import { Link2, UserPlus, Users } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { CopyButton } from '@/components/copy-button'
import { EmptyState, ErrorState, LoadingRows } from '@/components/states'
import { ApiError } from '@/lib/api'
import { MIN_PASSWORD_LENGTH } from '@/lib/auth-api'
import {
  absoluteLink,
  useAddUser,
  useChangeRole,
  useInviteUser,
  useResetLink,
  useSetDisabled,
  useUsers,
  type ArcUser,
  type OneTimeLink,
  type UserRole,
} from '@/lib/users'

const ROLES: UserRole[] = ['viewer', 'operator']

const errorText = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong.')

const roleOf = (user: ArcUser): UserRole => (user.roles.includes('operator') ? 'operator' : 'viewer')

function RoleSelect({
  value,
  onChange,
  label,
}: {
  value: UserRole
  onChange: (role: UserRole) => void
  label: string
}) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as UserRole)}>
      <SelectTrigger className="w-full sm:w-32" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {ROLES.map((r) => (
          <SelectItem key={r} value={r}>
            {r}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/** The one-time link, shown once with a copy button. */
function LinkBox({ link, title }: { link: OneTimeLink; title: string }) {
  const url = absoluteLink(link.link_path)
  return (
    <div className="space-y-2 rounded-lg border border-border bg-muted/30 p-3">
      <div className="text-sm font-medium text-foreground">
        {title} for {link.email}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 break-all rounded bg-muted px-2 py-1 text-xs">{url}</code>
        <CopyButton text={url} label="Copy link" />
      </div>
      <p className="text-xs text-muted-foreground">
        This link works once. It stops working after 72 hours or if Arc restarts.
      </p>
    </div>
  )
}

function FormShell({ children, error }: { children: ReactNode; error: string }) {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-card p-4">
      {children}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}

function AddPersonForm({ onDone }: { onDone: () => void }) {
  const add = useAddUser()
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState<UserRole>('viewer')
  const [password, setPassword] = useState('')

  const submit = () =>
    add.mutate(
      { email: email.trim(), password, role, display_name: name.trim() || undefined },
      { onSuccess: onDone },
    )

  return (
    <FormShell error={add.isError ? errorText(add.error) : ''}>
      <h3 className="text-sm font-semibold text-foreground">Add a person</h3>
      <Input type="email" placeholder="email" aria-label="Email" value={email} onChange={(e) => setEmail(e.target.value)} />
      <Input placeholder="name (optional)" aria-label="Name" value={name} onChange={(e) => setName(e.target.value)} />
      <RoleSelect value={role} onChange={setRole} label="Role" />
      <Input
        type="password"
        autoComplete="new-password"
        placeholder="password"
        aria-label="Password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
      />
      <p className="text-xs text-muted-foreground">At least {MIN_PASSWORD_LENGTH} characters.</p>
      <div className="flex gap-2">
        <Button onClick={submit} disabled={add.isPending || !email.trim() || !password}>
          {add.isPending ? 'Adding…' : 'Add person'}
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </FormShell>
  )
}

function InviteForm({ onLink, onDone }: { onLink: (link: OneTimeLink) => void; onDone: () => void }) {
  const invite = useInviteUser()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<UserRole>('viewer')

  const submit = () =>
    invite.mutate(
      { email: email.trim(), role },
      {
        onSuccess: (link) => {
          onLink(link)
          onDone()
        },
      },
    )

  return (
    <FormShell error={invite.isError ? errorText(invite.error) : ''}>
      <h3 className="text-sm font-semibold text-foreground">Invite someone</h3>
      <p className="text-xs text-muted-foreground">
        You get a link to send them. They open it and choose their own password.
      </p>
      <Input type="email" placeholder="email" aria-label="Invite email" value={email} onChange={(e) => setEmail(e.target.value)} />
      <RoleSelect value={role} onChange={setRole} label="Invite role" />
      <div className="flex gap-2">
        <Button onClick={submit} disabled={invite.isPending || !email.trim()}>
          {invite.isPending ? 'Creating link…' : 'Create invite link'}
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </FormShell>
  )
}

function UserRow({ user, onLink }: { user: ArcUser; onLink: (link: OneTimeLink) => void }) {
  const changeRole = useChangeRole()
  const setDisabled = useSetDisabled()
  const reset = useResetLink()
  const failure = [changeRole, setDisabled, reset].find((m) => m.isError)?.error
  const busy = changeRole.isPending || setDisabled.isPending || reset.isPending

  return (
    <li className="space-y-2 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-foreground">{user.email}</div>
          {user.display_name && (
            <div className="truncate text-xs text-muted-foreground">{user.display_name}</div>
          )}
        </div>
        <span
          className={
            user.disabled
              ? 'rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground'
              : 'rounded-sm border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400'
          }
        >
          {user.disabled ? 'Disabled' : 'Active'}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <RoleSelect
          value={roleOf(user)}
          label={`Role for ${user.email}`}
          onChange={(role) => changeRole.mutate({ email: user.email, role })}
        />
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => reset.mutate(user.email, { onSuccess: onLink })}
        >
          Reset password
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => setDisabled.mutate({ email: user.email, disabled: !user.disabled })}
        >
          {user.disabled ? 'Enable' : 'Disable'}
        </Button>
      </div>
      {failure && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(failure)}
        </p>
      )}
    </li>
  )
}

type Form = 'add' | 'invite' | null

/** People who can sign in to this Arc. Operators manage them; viewers see a notice. */
export function UsersPanel() {
  const query = useUsers()
  const [form, setForm] = useState<Form>(null)
  const [link, setLink] = useState<OneTimeLink | null>(null)

  if (query.isLoading) return <LoadingRows rows={3} />
  if (query.error instanceof ApiError && query.error.status === 403) {
    return (
      <EmptyState icon={<Users className="size-6" />} title="Only an operator can manage people." />
    )
  }
  if (query.isError) return <ErrorState error={query.error} />

  const users = query.data?.users ?? []
  const showLink = (next: OneTimeLink) => setLink(next)

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="min-w-0 flex-1 font-display text-[15px] font-bold text-foreground">People</h2>
        <Button size="sm" onClick={() => setForm('add')}>
          <UserPlus /> Add person
        </Button>
        <Button size="sm" variant="outline" onClick={() => setForm('invite')}>
          <Link2 /> Invite
        </Button>
      </div>
      {form === 'add' && <AddPersonForm onDone={() => setForm(null)} />}
      {form === 'invite' && <InviteForm onLink={showLink} onDone={() => setForm(null)} />}
      {link && <LinkBox link={link} title="Link" />}
      {users.length === 0 ? (
        <EmptyState title="No people yet" description="Add someone, or send them an invite link." />
      ) : (
        <ul className="space-y-2">
          {users.map((u) => (
            <UserRow key={u.id} user={u} onLink={showLink} />
          ))}
        </ul>
      )}
    </div>
  )
}
