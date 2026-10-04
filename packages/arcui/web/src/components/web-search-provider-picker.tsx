import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useSystemConfigFile } from '@/lib/queries'
import { apiPatch } from '@/lib/api'
import { errorText } from '@/lib/maintenance'

const SEARCH_PROVIDERS = [
  { value: 'tavily', label: 'Tavily' },
  { value: 'firecrawl', label: 'Firecrawl' },
  { value: 'parallel', label: 'Parallel' },
] as const

type SearchProvider = (typeof SEARCH_PROVIDERS)[number]['value']

type WebConfigSections = {
  modules?: { web?: { config?: { search_provider?: SearchProvider } } }
}

/** Which service web search uses, for every agent (`[modules.web.config] search_provider`
 *  in the fleet-wide settings). The key for it is set in the table above; an agent that
 *  has its own choice keeps it. */
export function WebSearchProviderPicker({ editable }: { editable: boolean }) {
  const queryClient = useQueryClient()
  const config = useSystemConfigFile('arcagent')
  const save = useMutation<unknown, Error, SearchProvider>({
    mutationFn: (value) =>
      apiPatch('/api/system-config/arcagent', {
        modules: { web: { config: { search_provider: value } } },
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['system', 'config', 'arcagent'] }),
  })

  const sections = (config.data?.sections ?? {}) as WebConfigSections
  const current = sections.modules?.web?.config?.search_provider

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card px-4 py-3">
      <span id="web-search-provider-label" className="text-sm font-medium text-foreground">
        Search service
      </span>
      <Select
        value={current ?? ''}
        onValueChange={(v) => save.mutate(v as SearchProvider)}
        disabled={!editable || save.isPending}
      >
        <SelectTrigger className="w-48" aria-labelledby="web-search-provider-label">
          <SelectValue placeholder="Not chosen yet" />
        </SelectTrigger>
        <SelectContent>
          {SEARCH_PROVIDERS.map((p) => (
            <SelectItem key={p.value} value={p.value}>
              {p.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {!editable && (
        <span className="text-xs italic text-muted-foreground/80">
          Enable operator mode to choose a service.
        </span>
      )}
      {save.isError && (
        <span role="alert" className="text-xs text-destructive">
          {errorText(save.error)}
        </span>
      )}
    </div>
  )
}
