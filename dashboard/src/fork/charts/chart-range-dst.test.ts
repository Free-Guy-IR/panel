import { afterAll, beforeAll, describe, expect, it } from 'bun:test'

import { buildPeriodOptions, getChartQueryRangeFromDateRange, getDateRangeForPeriodOption } from '@/utils/chart-period-utils'

const originalTz = process.env.TZ
const offsetOf = (value: string) => value.slice(-6)
const t = ((key: string, options?: { defaultValue?: string }) => options?.defaultValue ?? key) as never

beforeAll(() => {
  process.env.TZ = 'Europe/Berlin'
})

afterAll(() => {
  process.env.TZ = originalTz
})

describe('chart query ranges across a DST change', () => {
  it('a picked range from winter time into summer time sends one UTC offset and whole days', () => {
    const range = getChartQueryRangeFromDateRange({ from: new Date(2026, 2, 20, 15, 30), to: new Date(2026, 3, 5, 9, 0) })
    expect(new Date(2026, 2, 20).getTimezoneOffset()).not.toBe(new Date(2026, 3, 5).getTimezoneOffset())
    expect(offsetOf(range.startDate)).toBe(offsetOf(range.endDate))
    expect(range.startDate).toBe('2026-03-20T00:00:00+02:00')
    expect(range.endDate).toBe('2026-04-05T23:59:59+02:00')
  })

  it('every preset period sends start and end with the same UTC offset', () => {
    for (const option of buildPeriodOptions(t)) {
      const { startDate, endDate } = getDateRangeForPeriodOption(option)
      expect(`${option.value}:${offsetOf(startDate)}`).toBe(`${option.value}:${offsetOf(endDate)}`)
    }
  })
})
