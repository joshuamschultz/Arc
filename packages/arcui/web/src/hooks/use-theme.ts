import { useCallback, useEffect, useState } from 'react'

const STORAGE_KEY = 'arc-theme'

function getInitialDark(): boolean {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored !== null) return stored === 'dark'
  } catch {}
  return false // default: light
}

export function useTheme() {
  const [dark, setDark] = useState(getInitialDark)

  useEffect(() => {
    const root = document.documentElement
    if (dark) {
      root.classList.add('dark')
    } else {
      root.classList.remove('dark')
    }
    try {
      localStorage.setItem(STORAGE_KEY, dark ? 'dark' : 'light')
    } catch {}
  }, [dark])

  const toggle = useCallback(() => setDark(d => !d), [])

  return { dark, toggle }
}
