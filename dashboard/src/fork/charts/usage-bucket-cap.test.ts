import { describe, expect, it } from 'bun:test'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { Period } from '@/service/api'
import { USAGE_MAX_BUCKETS, USAGE_PERIOD_LENGTH_MS, capUsagePeriod, exceedsUsageBucketCap, usageBucketCount } from '@/fork/charts/usage-bucket-cap'
import { getChartQueryRangeFromDateRange, getChartQueryRangeFromShortcut } from '@/utils/chart-period-utils'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const repoRoot = join(dashboardRoot, '..')
const DAY_MS = 86_400_000
const start = '2026-01-01T00:00:00+00:00'
const plus = (ms: number) => new Date(Date.parse(start) + ms).toISOString()

describe('usage bucket counting matches the backend predicate', () => {
  it('34 days of minute buckets is 48,960 and allowed', () => {
    expect(usageBucketCount(Period.minute, start, plus(34 * DAY_MS))).toBe(48_960)
    expect(exceedsUsageBucketCap(Period.minute, start, plus(34 * DAY_MS))).toBe(false)
  })

  it('35 days of minute buckets is 50,400 and rejected', () => {
    expect(usageBucketCount(Period.minute, start, plus(35 * DAY_MS))).toBe(50_400)
    expect(exceedsUsageBucketCap(Period.minute, start, plus(35 * DAY_MS))).toBe(true)
  })

  it('exactly 50,000 minute buckets is allowed and 50,001 is rejected', () => {
    expect(exceedsUsageBucketCap(Period.minute, start, plus(50_000 * 60_000))).toBe(false)
    expect(exceedsUsageBucketCap(Period.minute, start, plus(50_000 * 60_000 + 1))).toBe(true)
    expect(exceedsUsageBucketCap(Period.minute, start, plus(50_001 * 60_000))).toBe(true)
  })

  it('hour, day and month buckets fit every realistic range', () => {
    const allTimeStart = '2000-01-01T00:00:00+00:00'
    const farEnd = '2030-12-31T23:59:59+00:00'
    expect(exceedsUsageBucketCap(Period.hour, start, plus(5 * 365 * DAY_MS))).toBe(false)
    expect(exceedsUsageBucketCap(Period.day, allTimeStart, farEnd)).toBe(false)
    expect(exceedsUsageBucketCap(Period.month, allTimeStart, farEnd)).toBe(false)
  })

  it('an unparsable date is left for the backend to reject', () => {
    expect(exceedsUsageBucketCap(Period.minute, 'not-a-date', start)).toBe(false)
  })

  it('capUsagePeriod keeps a fitting override and falls back to auto otherwise', () => {
    expect(capUsagePeriod(Period.minute, Period.day, start, plus(34 * DAY_MS))).toEqual({ period: Period.minute })
    expect(capUsagePeriod(Period.minute, Period.day, start, plus(35 * DAY_MS))).toEqual({ period: Period.day, cappedFrom: Period.minute })
    expect(capUsagePeriod(undefined, Period.day, start, plus(35 * DAY_MS))).toEqual({ period: Period.day })
  })
})

describe('the dashboard cap cannot drift from the backend', () => {
  const backend = readFileSync(join(repoRoot, 'app', 'operation', '__init__.py'), 'utf8')

  it('USAGE_MAX_BUCKETS equals the backend constant', () => {
    const match = backend.match(/^USAGE_MAX_BUCKETS\s*=\s*([\d_]+)\s*$/m)
    expect(match).not.toBeNull()
    expect(USAGE_MAX_BUCKETS).toBe(Number(match![1].replace(/_/g, '')))
  })

  it('the backend rejects only a bucket count strictly above USAGE_MAX_BUCKETS', () => {
    expect(backend).toMatch(/^\s*if period is not None and \(end_date - start_date\) \/ PERIOD_LENGTHS\[Period\(period\)\] > USAGE_MAX_BUCKETS:\s*$/m)
    expect(exceedsUsageBucketCap(Period.minute, start, plus(USAGE_MAX_BUCKETS * USAGE_PERIOD_LENGTH_MS[Period.minute]))).toBe(false)
    expect(exceedsUsageBucketCap(Period.hour, start, plus(USAGE_MAX_BUCKETS * USAGE_PERIOD_LENGTH_MS[Period.hour]))).toBe(false)
    expect(exceedsUsageBucketCap(Period.hour, start, plus(USAGE_MAX_BUCKETS * USAGE_PERIOD_LENGTH_MS[Period.hour] + 1))).toBe(true)
  })

  it('every period length equals the backend PERIOD_LENGTHS entry', () => {
    const block = backend.match(/^PERIOD_LENGTHS\s*=\s*\{([\s\S]*?)^\}/m)
    expect(block).not.toBeNull()
    const unitMs: Record<string, number> = { seconds: 1_000, minutes: 60_000, hours: 3_600_000, days: DAY_MS, weeks: 7 * DAY_MS }
    const entries = [...block![1].matchAll(/Period\.(\w+):\s*td\((\w+)=(\d+)\)/g)].map(([, period, unit, amount]) => {
      expect(unitMs[unit]).toBeDefined()
      return [period, Number(amount) * unitMs[unit]] as const
    })
    expect(Object.fromEntries(entries)).toEqual(USAGE_PERIOD_LENGTH_MS)
    expect(entries.map(([period]) => period).sort()).toEqual(Object.values(Period).sort())
  })
})

describe('chart query ranges stay under the cap', () => {
  const countOf = (range: { period: Period; startDate: string; endDate: string }) => usageBucketCount(range.period, range.startDate, range.endDate)

  it('a 35 day custom range with a minute override falls back to the auto period', () => {
    const range = getChartQueryRangeFromDateRange({ from: new Date(2026, 0, 1), to: new Date(2026, 1, 4) }, '1w', { minuteForOneHour: true, periodOverride: Period.minute })
    expect(range.period).toBe(Period.day)
    expect(range.cappedFrom).toBe(Period.minute)
    expect(countOf(range)).toBeLessThanOrEqual(USAGE_MAX_BUCKETS)
  })

  it('a 34 day custom range keeps the minute override', () => {
    const range = getChartQueryRangeFromDateRange({ from: new Date(2026, 0, 1), to: new Date(2026, 1, 3) }, '1w', { minuteForOneHour: true, periodOverride: Period.minute })
    expect(range.period).toBe(Period.minute)
    expect(range.cappedFrom).toBeUndefined()
    expect(countOf(range)).toBeLessThanOrEqual(USAGE_MAX_BUCKETS)
  })

  it('the all time shortcut with an hour override falls back to day buckets', () => {
    const range = getChartQueryRangeFromShortcut('all', new Date(2026, 10, 3, 12), { minuteForOneHour: true, periodOverride: Period.hour })
    expect(range.period).toBe(Period.day)
    expect(range.cappedFrom).toBe(Period.hour)
    expect(countOf(range)).toBeLessThanOrEqual(USAGE_MAX_BUCKETS)
  })

  it('the one month shortcut keeps a minute override', () => {
    const range = getChartQueryRangeFromShortcut('1m', new Date(2026, 10, 3, 12), { minuteForOneHour: true, periodOverride: Period.minute })
    expect(range.period).toBe(Period.minute)
    expect(range.cappedFrom).toBeUndefined()
  })

  it('every override on every shortcut produces a query the backend accepts', () => {
    const now = new Date(2026, 10, 3, 12)
    for (const shortcut of ['1h', '2h', '4h', '6h', '12h', '24h', '2d', '3d', '5d', '1w', '2w', '1m', '3m', 'all']) {
      for (const periodOverride of [undefined, Period.minute, Period.hour, Period.day, Period.month]) {
        const range = getChartQueryRangeFromShortcut(shortcut, now, { minuteForOneHour: true, periodOverride })
        expect(countOf(range)).toBeLessThanOrEqual(USAGE_MAX_BUCKETS)
      }
    }
  })
})

describe('every chart with a period selector reports a capped override', () => {
  const listSources = (dir: string): string[] =>
    readdirSync(dir).flatMap(name => {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) return listSources(path)
      return path.endsWith('.tsx') && !path.endsWith('.test.tsx') ? [path] : []
    })

  const selectorUses = listSources(join(dashboardRoot, 'src')).flatMap(file =>
    (readFileSync(file, 'utf8').match(/<PeriodSelector\b[^>]*\/>/g) ?? []).map(use => ({ file: relative(dashboardRoot, file), use })),
  )

  it('finds the chart period selectors', () => {
    expect(selectorUses.length).toBeGreaterThanOrEqual(8)
  })

  for (const { file, use } of selectorUses) {
    it(`${file} passes cappedFrom to its PeriodSelector`, () => {
      expect(use).toMatch(/\bcappedFrom=\{/)
    })
  }

  it('the node stats area chart caps its override against its own query range', () => {
    const source = readFileSync(join(dashboardRoot, 'src', 'components', 'charts', 'area-costume-chart.tsx'), 'utf8')
    expect(source).not.toMatch(/resolvePeriodOverride\(periodOverride\)\s*\?\?\s*periodOption\.period/)
    expect(source).toMatch(/capUsagePeriod\(/)
  })
})
