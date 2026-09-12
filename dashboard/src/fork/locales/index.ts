import en from './en.json'
import fa from './fa.json'
import ru from './ru.json'
import zh from './zh.json'

type LocaleTree = { [key: string]: unknown }

const overlays: Record<string, LocaleTree> = { en, fa, ru, zh }

function isPlain(value: unknown): value is LocaleTree {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const UNSAFE_KEYS = new Set(['__proto__', 'prototype', 'constructor'])

function mergeDeep(base: unknown, extra: unknown): unknown {
  if (!isPlain(extra)) {
    return extra === undefined ? base : extra
  }
  if (!isPlain(base)) {
    return extra
  }
  const out: LocaleTree = { ...base }
  for (const [key, value] of Object.entries(extra)) {
    if (UNSAFE_KEYS.has(key)) {
      continue
    }
    out[key] = mergeDeep(base[key], value)
  }
  return out
}

export function mergeForkLocale(base: LocaleTree, languages?: string | string[]): LocaleTree {
  const language = (Array.isArray(languages) ? languages[0] : languages) || 'en'
  const code = String(language).split('-')[0]
  const extra = overlays[code]
  if (!extra) {
    return base
  }
  return mergeDeep(base, extra) as LocaleTree
}
