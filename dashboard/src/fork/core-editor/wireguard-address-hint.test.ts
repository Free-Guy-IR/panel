import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { mergeForkLocale } from '@/fork/locales'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const languages = ['en', 'fa', 'ru', 'zh'] as const

const loadPublicLocale = (language: string) =>
  JSON.parse(readFileSync(join(dashboardRoot, 'public', 'statics', 'locales', `${language}.json`), 'utf8')) as Record<string, any>

const addressHintOf = (tree: Record<string, any>) => tree?.coreEditor?.inbound?.wireguard?.addressHint as string | undefined

const inboundsSectionSource = readFileSync(
  join(dashboardRoot, 'src', 'features', 'core-editor', 'components', 'xray', 'wireguard-inbound-fields.tsx'),
  'utf8',
)

describe('Xray WireGuard inbound server address example', () => {
  for (const language of languages) {
    it(`the ${language} hint shows a /24 interface subnet`, () => {
      const base = loadPublicLocale(language)
      const hint = addressHintOf(base)
      expect(hint).toBeDefined()
      expect(hint).toMatch(/10\.0\.0\.1\/24$/)
      expect(hint).not.toMatch(/\/(128|32)\b/)
    })

    it(`the ${language} hint the fork overlay serves is the same /24 example`, () => {
      const base = loadPublicLocale(language)
      const merged = mergeForkLocale(base, language) as Record<string, any>
      expect(addressHintOf(merged)).toBe(addressHintOf(base))
      expect(addressHintOf(merged)).toMatch(/10\.0\.0\.1\/24$/)
    })
  }

  it('the editor falls back to the same /24 example when the key is missing', () => {
    const call = inboundsSectionSource.match(/t\('coreEditor\.inbound\.wireguard\.addressHint',\s*\{\s*defaultValue:\s*'([^']*)'/)
    expect(call).not.toBeNull()
    expect(call?.[1]).toBe('Example: 10.0.0.1/24')
  })
})
