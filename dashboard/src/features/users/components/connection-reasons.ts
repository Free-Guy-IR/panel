import i18next, { type TFunction } from 'i18next'

/**
 * A reason as the backend records it: a code and the values behind it.
 *
 * The wording lives in the translation files rather than in the stored row, so
 * a row written months ago still reads in whatever language the panel is set
 * to now.
 */
export interface ConnectionReason {
  code?: string
  count?: number
  cycles?: number
  items?: string[]
}

/** Join with whatever separator the reader's language uses, not a fixed one. */
function joinItems(items: string[]): string {
  try {
    return new Intl.ListFormat(i18next.language, { style: 'narrow', type: 'unit' }).format(items)
  } catch {
    return items.join(', ')
  }
}

export function renderReason(reason: ConnectionReason | string, t: TFunction): string {
  // Rows written before reasons were structured are plain sentences. Showing
  // them as they are beats showing nothing while they age out.
  if (typeof reason === 'string') return reason
  if (!reason?.code) return ''

  return t(`connectionLimit.reason.${reason.code}`, {
    count: reason.count ?? 0,
    cycles: reason.cycles ?? 0,
    items: joinItems(reason.items ?? []),
    defaultValue: reason.code,
  })
}
