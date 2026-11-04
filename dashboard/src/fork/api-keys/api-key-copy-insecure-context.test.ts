import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'

const readSource = (path: string) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8')

const sources = {
  createDialog: { text: readSource('features/api-keys/dialogs/api-key-modal.tsx'), keyVar: 'createdKey' },
  reissueDialog: { text: readSource('pages/_dashboard.api-keys.tsx'), keyVar: 'newReissuedKey' },
}

const copyHandler = (text: string) => {
  const start = text.indexOf('const copyToClipboard')
  expect(start).toBeGreaterThan(-1)
  const next = text.indexOf('\n  const ', start + 1)
  return text.slice(start, next === -1 ? undefined : next)
}

describe('API key copy buttons on insecure contexts', () => {
  for (const [name, { text, keyVar }] of Object.entries(sources)) {
    it(`${name} copies through the shared clipboard hook with its insecure-context fallbacks`, () => {
      expect({ name, importsHook: text.includes("import { useClipboard } from '@/hooks/use-clipboard'") }).toEqual({ name, importsHook: true })
      expect({ name, usesHook: /const \{ copy \} = useClipboard\(\)/.test(text) }).toEqual({ name, usesHook: true })
      expect({ name, callsNavigatorClipboard: text.includes('navigator.clipboard') }).toEqual({ name, callsNavigatorClipboard: false })
    })

    it(`${name} waits for the copy and reports a failed copy instead of a false success`, () => {
      const handler = copyHandler(text)
      expect(handler).toContain('async () =>')
      expect(handler).toContain(`if (await copy(${keyVar}))`)
      expect(handler.indexOf(`if (await copy(${keyVar}))`)).toBeLessThan(handler.indexOf("toast.success(t('apiKeys.apiKeyCopySuccess'))"))
      expect(handler).toContain("toast.error(t('copyFailed', { defaultValue: 'Failed to copy content' }))")
    })
  }
})
