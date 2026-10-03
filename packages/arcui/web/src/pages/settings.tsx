import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { FieldHelp } from "@/components/help";
import { OperatorModeToggle } from "@/components/operator-mode-toggle";
import { RestartStackButton } from "@/components/restart-stack-button";
import { ContextNote } from "@/components/hitl";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { EmptyState } from "@/components/states";
import { KeysPanel } from "@/components/keys-panel";
import { MemoryPromotionPanel } from "@/components/memory-promotion-panel";
import { ConfigFilePanel } from "@/components/settings-view/config-file-panel";
import { PublicAccessPanel } from "@/components/settings-view/public-access-panel";
import { useOperatorMode } from "@/hooks/use-operator-mode";
import { useRoster } from "@/lib/queries";

// The three per-agent config files, one editor tab each.
const CONFIG_FILES = [
  { key: "arcllm", label: "ArcLLM" },
  { key: "arcrun", label: "ArcRun" },
  { key: "arcagent", label: "ArcAgent" },
] as const;

// gateway.toml exists only fleet-wide — it configures which chat surfaces this
// deployment has and who is allowed to talk to them, so there is no per-agent
// counterpart to layer over. It appears as a fourth tab under System only.
const SYSTEM_ONLY_FILES = [{ key: "gateway", label: "Gateway" }] as const;

// Sentinel scope: the fleet-wide `~/.arc` files that per-agent files layer over.
const SYSTEM_SCOPE = "__system__";

// Per-agent memory promotion settings; no fleet-wide counterpart.
const MEMORY_SHARING_TAB = "memory-sharing";

export function SettingsPage() {
  const roster = useRoster();
  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden);
  const [picked, setPicked] = useState<string | null>(null);
  const scope = picked ?? agents[0]?.agent_id ?? null;
  const isSystem = scope === SYSTEM_SCOPE;
  const [operatorMode] = useOperatorMode();
  // `?tab=access` lets a link (the sign-in warning) land on the Access tab directly.
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState(searchParams.get("tab") ?? "arcllm");
  // The Memory sharing tab exists only in agent scope; fall back when the
  // operator switches to System while it is open.
  const activeTab = isSystem && tab === MEMORY_SHARING_TAB ? "arcllm" : tab;

  const visibleFiles = isSystem
    ? [...CONFIG_FILES, ...SYSTEM_ONLY_FILES]
    : [...CONFIG_FILES];

  const currentAgent = agents.find((a) => a.agent_id === scope);
  const scopeName = isSystem
    ? "System (~/.arc)"
    : currentAgent?.display_name ||
      currentAgent?.name ||
      currentAgent?.agent_id ||
      "agent";
  const description = isSystem
    ? "Fleet-wide settings in ~/.arc. Each agent can layer its own values over these."
    : `Settings for ${scopeName}. These layer over the fleet-wide System defaults.`;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Settings"
        description={description}
        actions={
          <>
            <RestartStackButton />
            <OperatorModeToggle />
            <Select value={scope ?? ""} onValueChange={setPicked}>
              <SelectTrigger className="w-52">
                <SelectValue placeholder="Select scope" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={SYSTEM_SCOPE}>System (~/.arc)</SelectItem>
                {agents.length > 0 && <SelectSeparator />}
                {agents
                  .filter((a): a is typeof a & { agent_id: string } =>
                    Boolean(a.agent_id),
                  )
                  .map((a) => (
                    <SelectItem key={a.agent_id} value={a.agent_id}>
                      {a.display_name || a.name || a.agent_id}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
            <FieldHelp helpKey="settings.scope" route="settings" />
          </>
        }
      />
      {!scope ? (
        <div className="flex-1 overflow-auto p-4 md:p-6">
          <EmptyState
            title="No scope selected"
            description="Pick System (~/.arc) or an agent from the selector to view and edit its settings."
          />
        </div>
      ) : (
        <Tabs
          value={activeTab}
          onValueChange={setTab}
          className="flex flex-1 flex-col overflow-hidden"
        >
          <div className="border-b border-border px-4 md:px-6">
            <TabsList className="my-2">
              {visibleFiles.map((f) => (
                <TabsTrigger key={f.key} value={f.key}>
                  {f.label}
                </TabsTrigger>
              ))}
              {!isSystem && (
                <TabsTrigger value={MEMORY_SHARING_TAB}>
                  Memory sharing
                </TabsTrigger>
              )}
              {/* Keys live in the fleet-wide `~/.arc/.env`, not in any config
                  file — the tab shows in every scope so a fresh install finds
                  it without first knowing to switch to System. */}
              <TabsTrigger value="keys">Keys</TabsTrigger>
              {/* Where the dashboard is reached from; sign-in return addresses
                  and the https certificate hang off it, so it is fleet-wide too. */}
              <TabsTrigger value="access">Access</TabsTrigger>
            </TabsList>
          </div>
          {visibleFiles.map((f) => (
            <TabsContent
              key={f.key}
              value={f.key}
              className="flex-1 overflow-auto p-4 md:p-6"
            >
              <ConfigFilePanel
                system={isSystem}
                agentId={scope}
                file={f.key}
                label={f.label}
                editable={operatorMode}
              />
            </TabsContent>
          ))}
          {!isSystem && (
            <TabsContent
              value={MEMORY_SHARING_TAB}
              className="flex-1 overflow-auto p-4 md:p-6"
            >
              <div className="mx-auto max-w-xl">
                <MemoryPromotionPanel
                  key={scope}
                  agentId={scope}
                  operatorMode={operatorMode}
                />
              </div>
            </TabsContent>
          )}
          <TabsContent value="access" className="flex-1 overflow-auto p-4 md:p-6">
            <PublicAccessPanel editable={operatorMode} />
          </TabsContent>
          <TabsContent value="keys" className="flex-1 overflow-auto p-4 md:p-6">
            <div className="mx-auto max-w-5xl space-y-4">
              <div className="flex items-start gap-3">
                <span className="mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg border border-border bg-muted/40 text-muted-foreground">
                  <KeyRound className="size-4" />
                </span>
                <div className="min-w-0">
                  <h2 className="font-display text-[15px] font-bold text-foreground">
                    Provider keys
                  </h2>
                  <p className="mt-0.5 text-[13px] leading-snug text-muted-foreground">
                    The API keys your agents use to reach each AI provider.
                  </p>
                </div>
              </div>
              <ContextNote tone="info">
                Keys are fleet-wide — stored in{" "}
                <span className="font-mono">~/.arc/.env</span> and shared by
                every agent, whichever scope is selected above. A key is never
                shown back to you; this panel only reports whether one is set.
              </ContextNote>
              <KeysPanel editable={operatorMode} />
            </div>
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}
