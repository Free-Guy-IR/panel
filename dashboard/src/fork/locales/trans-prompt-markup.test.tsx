import { describe, expect, it } from 'bun:test'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import i18next from 'i18next'
import { createElement, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { Trans } from 'react-i18next'
import { mergeForkLocale } from '@/fork/locales'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const srcRoot = join(dashboardRoot, 'src')
const appI18nSource = readFileSync(join(srcRoot, 'locales', 'i18n.ts'), 'utf8')
const appI18nCode = appI18nSource.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
const appEscapeValues = [...appI18nCode.matchAll(/\bescapeValue:\s*(\w+)/g)].map(match => match[1])
const appEscapeValue = appEscapeValues.length === 1 && /interpolation:\s*\{[^}]*escapeValue:\s*true/.test(appI18nCode)
const appKeepBasicHtmlNodeMentions = appI18nCode.match(/\btransKeepBasicHtmlNodesFor\b/g)?.length ?? 0
const appKeepBasicHtmlNodes = appI18nCode.match(/react:\s*\{[^}]*transKeepBasicHtmlNodesFor:\s*\[([^\]]*)\]/)?.[1]
const appKeepBasicHtmlNodesFor = appKeepBasicHtmlNodes === undefined ? undefined : [...appKeepBasicHtmlNodes.matchAll(/'([^']+)'/g)].map(match => match[1])
const appReactOptions = appKeepBasicHtmlNodesFor === undefined ? {} : { transKeepBasicHtmlNodesFor: appKeepBasicHtmlNodesFor }
const languages = ['en', 'fa', 'ru', 'zh'] as const

const loadLocale = (lng: string) => mergeForkLocale(JSON.parse(readFileSync(join(dashboardRoot, 'public', 'statics', 'locales', `${lng}.json`), 'utf8')), lng)

const makeI18n = async (lng: string) => {
  const instance = i18next.createInstance()
  await instance.init({
    lng,
    fallbackLng: 'en',
    resources: { [lng]: { translation: loadLocale(lng) }, en: { translation: loadLocale('en') } },
    interpolation: { escapeValue: appEscapeValue },
    react: appReactOptions,
  })
  return instance
}

const listSources = (dir: string): string[] =>
  readdirSync(dir).flatMap(name => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return listSources(path)
    return path.endsWith('.tsx') && !path.endsWith('.test.tsx') ? [path] : []
  })

type CallSite = { file: string; keys: string[]; components: string[] }

const findCallSites = (): CallSite[] =>
  listSources(srcRoot).flatMap(file => {
    const source = readFileSync(file, 'utf8')
    const blocks = source.match(/<Trans\b[\s\S]*?(?:shouldUnescape\s*\/>|\n\s*\/>)/g) ?? []
    return blocks.flatMap(block => {
      const literal = block.match(/i18nKey=["']([^"']+)["']/) ?? block.match(/i18nKey=\{["']([^"']+)["']\}/)
      let keys: string[] = []
      if (literal) {
        keys = [literal[1]]
      } else {
        const expression = block.match(/i18nKey=\{([^}]+)\}/)?.[1] ?? ''
        const identifier = expression.trim().match(/^[A-Za-z_$][\w$]*$/)?.[0]
        const resolved = identifier ? (source.match(new RegExp(`const ${identifier} = ([^\\n]+)`))?.[1] ?? '') : expression
        keys = [...resolved.matchAll(/'([^']+)'/g)].map(match => match[1])
      }
      const componentsBody = block.match(/components=\{\{([\s\S]*)\}\}/)?.[1] ?? ''
      const components = [...componentsBody.matchAll(/(?:^|,|\{)\s*([a-z][a-z0-9]*)\s*:/g)].map(match => match[1])
      return keys.length ? [{ file: relative(dashboardRoot, file), keys, components }] : []
    })
  })

const componentsFor = (names: string[]) => Object.fromEntries(names.map(name => [name, createElement(name)])) as Record<string, ReactElement>

const renderPrompt = async (lng: string, site: CallSite, key: string, name: string) => {
  const i18n = await makeI18n(lng)
  return renderToStaticMarkup(<Trans i18n={i18n} i18nKey={key} values={{ name, username: name }} components={componentsFor(site.components)} shouldUnescape />)
}

const callSites = findCallSites()
const siteFor = (key: string) => {
  const site = callSites.find(candidate => candidate.keys.includes(key))
  if (!site) throw new Error(`no <Trans> call site renders ${key}`)
  return site
}

describe('name prompts rendered through <Trans>', () => {
  it('the app i18n config keeps escapeValue on and sets one Trans keep list of exactly br, strong, i, p and b', () => {
    expect(appEscapeValue).toBe(true)
    expect(appKeepBasicHtmlNodeMentions).toBe(1)
    expect(appKeepBasicHtmlNodesFor).toEqual(['br', 'strong', 'i', 'p', 'b'])
    expect(appI18nCode.match(/transKeepBasicHtmlNodesFor:\s*(\[[^\]]*\])\s*[,}]/)?.[1].replace(/\s/g, '')).toBe("['br','strong','i','p','b']")
    expect(appI18nCode).not.toMatch(/\btransSupportBasicHtmlNodes\b/)
  })

  it('the scan finds the node and host delete prompts', () => {
    expect(siteFor('deleteNode.prompt').file).toBe('src/features/nodes/components/node-actions-menu.tsx')
    expect(siteFor('deleteHost.prompt').file).toBe('src/features/hosts/components/host-actions-menu.tsx')
  })

  for (const site of callSites) {
    for (const key of site.keys) {
      for (const lng of languages) {
        it(`${site.file} ${key} [${lng}] shows no markup as literal text`, async () => {
          const markup = await renderPrompt(lng, site, key, 'node-1')
          expect(markup).not.toContain('&lt;')
          expect(markup).not.toContain('&gt;')
        })
      }
    }
  }

  for (const key of ['deleteNode.prompt', 'deleteHost.prompt']) {
    for (const lng of ['fa', 'ru', 'zh']) {
      it(`${key} [${lng}] renders the <b> of its translation as a bold element with no components prop`, async () => {
        const i18n = await makeI18n(lng)
        const markup = renderToStaticMarkup(<Trans i18n={i18n} i18nKey={key} values={{ name: 'node-1' }} shouldUnescape />)
        expect(markup).toContain('<b>node-1</b>')
        expect(markup).not.toContain('&lt;')
        expect(markup).not.toContain('&gt;')
      })

      it(`${key} [${lng}] with no components prop renders a hostile name as text`, async () => {
        const hostile = '<img src=x onerror=alert(1)><b>owned</b></b><script>alert(2)</script>'
        const i18n = await makeI18n(lng)
        const markup = renderToStaticMarkup(<Trans i18n={i18n} i18nKey={key} values={{ name: hostile }} shouldUnescape />)
        expect(markup).not.toContain('<img')
        expect(markup).not.toContain('<script')
        expect(markup).not.toContain('<b>owned</b>')
        expect(markup).toContain('&lt;img src=x onerror=alert(1)&gt;&lt;b&gt;owned&lt;/b&gt;&lt;/b&gt;&lt;script&gt;alert(2)&lt;/script&gt;')
        expect((markup.match(/<\/?b>/g) ?? []).length).toBe(2)
      })
    }

    it(`${key} [fa] wraps the name in a bold element`, async () => {
      const markup = await renderPrompt('fa', siteFor(key), key, 'node-1')
      expect(markup).toMatch(/<(b|strong)>node-1<\/(b|strong)>/)
    })

    for (const lng of languages) {
      it(`${key} [${lng}] renders a hostile name as text`, async () => {
        const hostile = '<img src=x onerror=alert(1)></b><script>alert(2)</script>'
        const markup = await renderPrompt(lng, siteFor(key), key, hostile)
        expect(markup).not.toContain('<img')
        expect(markup).not.toContain('<script')
        expect(markup).toContain('&lt;img src=x onerror=alert(1)&gt;&lt;/b&gt;&lt;script&gt;alert(2)&lt;/script&gt;')
        expect((markup.match(/<\/?(b|strong)>/g) ?? []).length).toBeLessThanOrEqual(2)
      })
    }
  }
})
