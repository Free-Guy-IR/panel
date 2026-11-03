import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import i18next from 'i18next'
import { mergeForkLocale } from '@/fork/locales'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const hookSource = readFileSync(join(dashboardRoot, 'src', 'fork', 'charts', 'use-usage-period-cap-notice.ts'), 'utf8')
const messageKey = hookSource.match(/\bt\(\s*'([^']+)'/)?.[1]

describe('the capped period message', () => {
  it('the notice hook reads a translation key', () => {
    expect(messageKey).toBe('statistics.periodCapped')
  })

  for (const lng of ['en', 'fa', 'ru', 'zh']) {
    it(`is translated in ${lng} with the period and the cap filled in`, async () => {
      const instance = i18next.createInstance()
      const translation = mergeForkLocale(JSON.parse(readFileSync(join(dashboardRoot, 'public', 'statics', 'locales', `${lng}.json`), 'utf8')), lng)
      await instance.init({ lng, resources: { [lng]: { translation } }, interpolation: { escapeValue: true } })
      expect(instance.exists(messageKey!)).toBe(true)
      const text = instance.t(messageKey!, { period: 'PERIOD', max: '50,000' })
      expect(text).toContain('PERIOD')
      expect(text).toContain('50,000')
    })
  }
})
