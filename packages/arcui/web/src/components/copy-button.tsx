import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'

// arcui is normally served over plain http on a LAN address, where the browser
// withholds `navigator.clipboard` entirely. The off-screen textarea is the only
// copy a non-secure context allows, so it is the working path here, not a
// fallback for old browsers.
async function writeToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* permission denied — try the selection path before giving up */
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  document.body.appendChild(area)
  area.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(area)
  return copied
}

/** Copies `text`, and says so — including when the browser refuses, so nobody
 *  walks away believing they have something they don't. */
export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  const copy = () => {
    void writeToClipboard(text).then((ok) => {
      setState(ok ? 'copied' : 'failed')
      window.setTimeout(() => setState('idle'), 2500)
    })
  }

  return (
    <Button variant="outline" size="xs" type="button" onClick={copy}>
      {state === 'copied' ? <Check /> : <Copy />}
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy blocked' : label}
    </Button>
  )
}
