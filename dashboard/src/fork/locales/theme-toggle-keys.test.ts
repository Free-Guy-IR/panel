import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import i18next from 'i18next'
import { mergeForkLocale } from '@/fork/locales'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const themeToggleSource = readFileSync(join(dashboardRoot, 'src', 'components', 'common', 'theme-toggle.tsx'), 'utf8')
const literalKeys = [...new Set([...themeToggleSource.matchAll(/\bt\(\s*'([^']+)'/g)].map(match => match[1]))]

const makeI18n = async (lng: string) => {
  const instance = i18next.createInstance()
  const translation = mergeForkLocale(JSON.parse(readFileSync(join(dashboardRoot, 'public', 'statics', 'locales', `${lng}.json`), 'utf8')), lng)
  await instance.init({ lng, resources: { [lng]: { translation } }, interpolation: { escapeValue: true } })
  return instance
}

describe('theme toggle translation keys', () => {
  it('the scan finds the screen-reader label key', () => {
    expect(literalKeys).toContain('theme.toggle')
  })

  for (const lng of ['en', 'fa', 'ru', 'zh']) {
    for (const key of literalKeys) {
      it(`${key} resolves to a string in ${lng}`, async () => {
        const i18n = await makeI18n(lng)
        expect(i18n.exists(key)).toBe(true)
        expect(typeof i18n.t(key)).toBe('string')
        expect(i18n.t(key)).not.toBe(key)
      })
    }
  }
})
