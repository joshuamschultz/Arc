import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// Self-hosted fonts (no CDN — federal/SCIF constraint). Bundled by Vite.
// Latin subset only — English UI; avoids shipping cyrillic/greek/vietnamese.
import '@fontsource/plus-jakarta-sans/latin-400.css'
import '@fontsource/plus-jakarta-sans/latin-500.css'
import '@fontsource/plus-jakarta-sans/latin-600.css'
import '@fontsource/plus-jakarta-sans/latin-700.css'
import '@fontsource/plus-jakarta-sans/latin-800.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import '@fontsource/ibm-plex-mono/latin-600.css'

import './index.css'
import App from './App.tsx'
import { bootstrapAuth } from '@/lib/auth'

// Consume `#auth=<token>` from the URL before anything else renders.
bootstrapAuth()

// Dark-only for v1; the theme tokens carry a light variant for a future toggle.
document.documentElement.classList.add('dark')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
