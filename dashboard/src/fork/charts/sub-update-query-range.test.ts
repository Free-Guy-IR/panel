import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { Period } from '@/service/api'
import { getSubUpdateShortcutQueryRange } from '@/fork/charts/sub-update-query-range'
import { USAGE_MAX_BUCKETS, usageBucketCount } from '@/fork/charts/usage-bucket-cap'

const now = new Date(2026, 10, 3, 12)
const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const countOf = (range: { period: Period; startDate: string; endDate: string }) => usageBucketCount(range.period, range.startDate, range.endDate)

describe('the subscription update chart treats a capped override as its own Auto', () => {
  it('All Time without an override queries month buckets', () => {
    const range = getSubUpdateShortcutQueryRange('all', now, undefined)
    expect(range.period).toBe(Period.month)
    expect(range.cappedFrom).toBeUndefined()
  })

  it('All Time with an hour override is capped to month buckets and reports the cap', () => {
    const range = getSubUpdateShortcutQueryRange('all', now, Period.hour)
    expect(range.period).toBe(Period.month)
    expect(range.cappedFrom).toBe(Period.hour)
    expect(countOf(range)).toBeLessThanOrEqual(USAGE_MAX_BUCKETS)
  })

  it('All Time with a minute override is capped to month buckets and reports the cap', () => {
    const range = getSubUpdateShortcutQueryRange('all', now, Period.minute)
    expect(range.period).toBe(Period.month)
    expect(range.cappedFrom).toBe(Period.minute)
    expect(countOf(range)).toBeLessThanOrEqual(USAGE_MAX_BUCKETS)
  })

  it('the capped All Time range queries the same window as Auto', () => {
    const auto = getSubUpdateShortcutQueryRange('all', now, undefined)
    const capped = getSubUpdateShortcutQueryRange('all', now, Period.hour)
    expect({ startDate: capped.startDate, endDate: capped.endDate }).toEqual({ startDate: auto.startDate, endDate: auto.endDate })
  })

  it('All Time with a fitting day override keeps day buckets', () => {
    const range = getSubUpdateShortcutQueryRange('all', now, Period.day)
    expect(range.period).toBe(Period.day)
    expect(range.cappedFrom).toBeUndefined()
  })

  it('a fitting override on a short shortcut keeps the override', () => {
    const minute = getSubUpdateShortcutQueryRange('1w', now, Period.minute)
    expect(minute.period).toBe(Period.minute)
    expect(minute.cappedFrom).toBeUndefined()
    const hour = getSubUpdateShortcutQueryRange('24h', now, Period.hour)
    expect(hour.period).toBe(Period.hour)
    expect(hour.cappedFrom).toBeUndefined()
  })

  it('a short shortcut without an override keeps its shortcut period', () => {
    expect(getSubUpdateShortcutQueryRange('1h', now, undefined).period).toBe(Period.minute)
    expect(getSubUpdateShortcutQueryRange('12h', now, undefined).period).toBe(Period.hour)
    expect(getSubUpdateShortcutQueryRange('1m', now, undefined).period).toBe(Period.day)
  })

  it('the chart builds its shortcut range through the fork helper', () => {
    const source = readFileSync(join(dashboardRoot, 'src', 'components', 'charts', 'user-sub-update-pie-chart.tsx'), 'utf8')
    expect(source).toMatch(/getSubUpdateShortcutQueryRange\(selectedTime, new Date\(\), resolvedOverride\)/)
    expect(source).not.toMatch(/getChartQueryRangeFromShortcut\(selectedTime/)
  })
})
