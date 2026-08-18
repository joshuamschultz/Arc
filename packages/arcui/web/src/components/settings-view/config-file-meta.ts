import { Cpu, RefreshCw, Bot, MessagesSquare } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

// Plain-language framing for each config file. The tab keeps its technical
// label (it maps to a real `<file>.toml`), but a business operator gets a
// human title and a one-line blurb of what that file actually controls.
export interface ConfigFileMeta {
  title: string
  blurb: string
  icon: LucideIcon
}

export const CONFIG_FILE_META: Record<string, ConfigFileMeta> = {
  arcllm: {
    title: 'Models & providers',
    blurb: 'Which AI models this agent uses and how it reaches each provider.',
    icon: Cpu,
  },
  arcrun: {
    title: 'Work loop',
    blurb: 'How the agent runs a job: turn limits, timeouts, and retries.',
    icon: RefreshCw,
  },
  arcagent: {
    title: 'Agent behavior',
    blurb: 'The agent itself — its tools, skills, memory, and modules.',
    icon: Bot,
  },
  gateway: {
    title: 'Chat surfaces',
    blurb: 'Which chat channels this deployment exposes and who may talk to them.',
    icon: MessagesSquare,
  },
}
