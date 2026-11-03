import { Period } from '@/service/api'
import { getChartQueryRangeFromShortcut } from '@/utils/chart-period-utils'

export const getSubUpdateShortcutQueryRange = (selectedTime: string, now: Date, periodOverride?: Period) => {
  const range = getChartQueryRangeFromShortcut(selectedTime, now, { minuteForOneHour: true, periodOverride })
  if (selectedTime === 'all' && (!periodOverride || range.cappedFrom)) return { ...range, period: Period.month }
  return range
}
