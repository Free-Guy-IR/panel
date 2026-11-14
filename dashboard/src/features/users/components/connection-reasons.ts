import i18next, { type TFunction } from 'i18next'

export interface ConnectionReason {
  code?: string
  count?: number
  cycles?: number
  items?: string[]
}

function joinItems(items: string[]): string {
  try {
    return new Intl.ListFormat(i18next.language, { style: 'narrow', type: 'unit' }).format(items)
  } catch {
    return items.join(', ')
  }
}

export function renderReason(reason: ConnectionReason | string, t: TFunction): string {
  if (typeof reason === 'string') return reason
  if (!reason?.code) return ''

  return t(`connectionLimit.reason.${reason.code}`, {
    count: reason.count ?? 0,
    cycles: reason.cycles ?? 0,
    items: joinItems(reason.items ?? []),
    defaultValue: reason.code,
  })
}
