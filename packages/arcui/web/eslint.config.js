import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // Rest-sibling destructuring is the idiom for omitting a key
      // (`const { drop, ...rest } = obj`); don't flag the intentionally
      // unused sibling.
      '@typescript-eslint/no-unused-vars': ['error', { ignoreRestSiblings: true }],
      // crypto.randomUUID is undefined outside a secure context (a plain-http
      // dashboard on a tailnet IP), and calling it there silently killed chat.
      'no-restricted-properties': [
        'error',
        { object: 'crypto', property: 'randomUUID', message: 'Use newRequestId() from @/lib/request-id.' },
      ],
    },
  },
  {
    files: ['src/lib/request-id.ts', '**/*.test.{ts,tsx}'],
    rules: { 'no-restricted-properties': 'off' },
  },
  {
    // shadcn/ui primitives co-locate a component with its CVA variants
    // (e.g. `buttonVariants`); that's the upstream convention, so the
    // fast-refresh "only export components" rule doesn't apply here.
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
])
