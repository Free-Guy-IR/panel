import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import i18next from 'i18next'
import { renderToStaticMarkup } from 'react-dom/server'
import { Trans } from 'react-i18next'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const appI18nSource = readFileSync(join(dashboardRoot, 'src', 'locales', 'i18n.ts'), 'utf8')
const appI18nCode = appI18nSource.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
const englishResources = JSON.parse(readFileSync(join(dashboardRoot, 'public', 'statics', 'locales', 'en.json'), 'utf8'))

const appEscapeValues = [...appI18nCode.matchAll(/\bescapeValue:\s*(\w+)/g)].map(match => match[1])
const appEscapeValue = appEscapeValues.length === 1 && /interpolation:\s*\{[^}]*escapeValue:\s*true/.test(appI18nCode)
const appKeepBasicHtmlNodes = appI18nCode.match(/react:\s*\{[^}]*transKeepBasicHtmlNodesFor:\s*\[([^\]]*)\]/)?.[1]
const appKeepBasicHtmlNodesFor = appKeepBasicHtmlNodes === undefined ? undefined : [...appKeepBasicHtmlNodes.matchAll(/'([^']+)'/g)].map(match => match[1])
const appReactOptions = appKeepBasicHtmlNodesFor === undefined ? {} : { transKeepBasicHtmlNodesFor: appKeepBasicHtmlNodesFor }

const makeI18n = async () => {
  const instance = i18next.createInstance()
  await instance.init({
    lng: 'en',
    fallbackLng: 'en',
    resources: { en: { translation: englishResources } },
    interpolation: { escapeValue: appEscapeValue },
    react: appReactOptions,
  })
  return instance
}

const hostileName = '<img src=x onerror=alert(1)><b>owned</b></b><script>alert(2)</script>'

describe('confirmation prompts with an admin-settable name', () => {
  it('the app i18n config keeps escapeValue on', () => {
    expect(appEscapeValue).toBe(true)
  })

  for (const key of ['deleteAdmin.prompt', 'adminRoles.deleteConfirm', 'templates.deleteUserTemplatePrompt', 'deleteNode.prompt', 'group.deleteConfirm']) {
    for (const components of [{ b: <b /> }, undefined]) {
      it(`${key} renders markup in the name as text ${components ? 'with' : 'without'} a b component`, async () => {
        const i18n = await makeI18n()
        const markup = renderToStaticMarkup(<Trans i18n={i18n} i18nKey={key} values={{ name: hostileName }} components={components} shouldUnescape />)

        expect(markup).not.toContain('<img')
        expect(markup).not.toContain('<script')
        expect(markup).not.toContain('<b>owned</b>')
        expect(markup).toContain('&lt;img src=x onerror=alert(1)&gt;&lt;b&gt;owned&lt;/b&gt;&lt;/b&gt;&lt;script&gt;alert(2)&lt;/script&gt;')
        expect((markup.match(/<b>/g) ?? []).length).toBeLessThanOrEqual(1)
      })
    }
  }
})
