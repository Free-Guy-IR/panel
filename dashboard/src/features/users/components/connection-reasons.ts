import type { TFunction } from 'i18next'

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
  items?: string[]
}

export function renderReason(reason: ConnectionReason | string, t: TFunction): string {
  // Rows written before reasons were structured are plain sentences. Showing
  // them as they are beats showing nothing while they age out.
  if (typeof reason === 'string') return reason
  if (!reason?.code) return ''

  const count = reason.count ?? 0
  const items = (reason.items ?? []).join('، ')

  return t(`connectionLimit.reason.${reason.code}`, {
    count,
    items,
    defaultValue: reason.code,
  })
}
