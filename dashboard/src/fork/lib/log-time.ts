type Instant = string | number | Date | null | undefined

const JALALI = 'fa-IR-u-ca-persian'
const GREGORIAN = 'en-GB'

const cache = new Map<string, Intl.DateTimeFormat>()

function formatter(locale: string, options: Intl.DateTimeFormatOptions, key: string): Intl.DateTimeFormat {
  const id = `${locale}|${key}`
  let found = cache.get(id)
  if (!found) {
    found = new Intl.DateTimeFormat(locale, options)
    cache.set(id, found)
  }
  return found
}

function localeFor(language: string | undefined): string {
  return (language ?? '').toLowerCase().startsWith('fa') ? JALALI : GREGORIAN
}

function toDate(value: Instant): Date | null {
  if (value == null) return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function render(value: Instant, language: string | undefined, options: Intl.DateTimeFormatOptions, key: string, fallback: string): string {
  const date = toDate(value)
  if (!date) return fallback
  return formatter(localeFor(language), options, key).format(date)
}

export function logTime(value: Instant, language?: string, fallback = '—'): string {
  return render(value, language, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }, 'time', fallback)
}

export function logDayTime(value: Instant, language?: string, fallback = '—'): string {
  return render(
    value,
    language,
    { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false },
    'daytime',
    fallback,
  )
}

export function logShort(value: Instant, language?: string, fallback = '—'): string {
  return render(value, language, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }, 'short', fallback)
}
