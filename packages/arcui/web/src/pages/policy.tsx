import { useMemo, useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { QueryState, EmptyState } from '@/components/states'
import { PolicyBulletCard } from '@/components/policy-bullet'
import { filterBullets, sortBullets, type BulletSort } from '@/lib/policy'
import { useTeamPolicyBullets, useTeamPolicyStats } from '@/lib/queries'

export function PolicyPage() {
  const bulletsQuery = useTeamPolicyBullets()
  const statsQuery = useTeamPolicyStats()
  const [search, setSearch] = useState('')
  const [hideRetired, setHideRetired] = useState(false)
  const [sort, setSort] = useState<BulletSort>('score')

  const stats = statsQuery.data
  const visible = useMemo(() => {
    const all = bulletsQuery.data?.bullets ?? []
    return sortBullets(filterBullets(all, { text: search, hideRetired }), sort)
  }, [bulletsQuery.data, search, hideRetired, sort])

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Policy" description="Fleet-wide ACE policy bullets." />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Total" value={stats?.total ?? 0} icon={<ShieldCheck className="size-4" />} />
          <StatCard label="Active" value={stats?.active ?? 0} />
          <StatCard label="Retired" value={stats?.retired ?? 0} />
          <StatCard label="Avg score" value={(stats?.avg_score ?? 0).toFixed(2)} />
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search bullets…"
            className="max-w-xs"
          />
          <Select value={sort} onValueChange={(v) => setSort(v as BulletSort)}>
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="score">Sort: score</SelectItem>
              <SelectItem value="uses">Sort: uses</SelectItem>
              <SelectItem value="created">Sort: newest</SelectItem>
            </SelectContent>
          </Select>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={hideRetired}
              onChange={(e) => setHideRetired(e.target.checked)}
              className="accent-primary"
            />
            Hide retired
          </label>
        </div>

        <QueryState
          query={bulletsQuery}
          isEmpty={() => (bulletsQuery.data?.bullets ?? []).length === 0}
          empty={<EmptyState icon={<ShieldCheck className="size-7" />} title="No policy bullets yet" />}
        >
          {() =>
            visible.length === 0 ? (
              <EmptyState title="No bullets match your filter" />
            ) : (
              <div className="space-y-2">
                {visible.map((b, i) => (
                  <PolicyBulletCard key={i} bullet={b} />
                ))}
              </div>
            )
          }
        </QueryState>
      </div>
    </div>
  )
}
