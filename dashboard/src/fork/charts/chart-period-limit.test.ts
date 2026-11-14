import { describe, expect, it } from 'bun:test'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { TRAFFIC_TIME_SELECTOR_SHORTCUTS } from '@/components/charts/time-selector'
import { Period } from '@/service/api'
import {
  CHART_PERIOD_OVERRIDE_AUTO,
  type ChartPeriodOverride,
  USAGE_RECORD_BUCKET_MINUTES,
  getAllowedChartPeriods,
  getChartQueryRangeFromDateRange,
  getChartQueryRangeFromShortcut,
  resolvePeriodOverride,
} from '@/utils/chart-period-utils'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const repoRoot = join(dashboardRoot, '..')
const inboundChartSource = readFileSync(join(dashboardRoot, 'src', 'features', 'statistics', 'components', 'inbound-usage-chart.tsx'), 'utf8')
const backendStats = readFileSync(join(repoRoot, 'app', 'models', 'stats.py'), 'utf8')

const backendMaxBuckets = Number(backendStats.match(/^MAX_USAGE_BUCKETS\s*=\s*([\d_]+)\s*$/m)?.[1].replace(/_/g, ''))
const backendRowMinutes = Number(backendStats.match(/^USAGE_RECORD_GRANULARITY\s*=\s*timedelta\(minutes=(\d+)\)\s*$/m)?.[1])
const backendLimitedPeriodMinutes: Partial<Record<Period, number>> = { [Period.minute]: 1, [Period.hour]: 60 }

const backendUsageBuckets = (range: { period: Period; startDate: string; endDate: string }) => {
  const periodMinutes = backendLimitedPeriodMinutes[range.period]
  if (periodMinutes === undefined) return 0
  const spanMinutes = (Date.parse(range.endDate) - Date.parse(range.startDate)) / 60_000
  return Math.ceil(spanMinutes / Math.max(periodMinutes, backendRowMinutes))
}

const listSources = (dir: string): string[] =>
  readdirSync(dir).flatMap(name => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return listSources(path)
    return path.endsWith('.tsx') && !path.endsWith('.test.tsx') ? [path] : []
  })

const selectorUses = listSources(join(dashboardRoot, 'src')).flatMap(file =>
  (readFileSync(file, 'utf8').match(/<PeriodSelector\b[^>]*\/>/g) ?? []).map(use => ({ file: relative(dashboardRoot, file), use })),
)

const now = new Date(2026, 10, 14, 12)
const customRanges = [1, 2, 3, 4, 7, 30, 34, 35, 379].map(days => ({ days, range: { from: new Date(2026, 0, 1), to: new Date(2026, 0, days) } }))

describe('every chart period selector offers only the periods its range allows', () => {
  it('finds the chart period selectors, the inbound usage chart among them', () => {
    expect(selectorUses.length).toBeGreaterThanOrEqual(9)
    expect(selectorUses.some(({ file }) => file === join('src', 'features', 'statistics', 'components', 'inbound-usage-chart.tsx'))).toBe(true)
  })

  for (const { file, use } of selectorUses) {
    it(`${file} passes allowedPeriods to its PeriodSelector`, () => {
      expect({ file, allowedPeriods: /\ballowedPeriods=\{allowedPeriods\}/.test(use) }).toEqual({ file, allowedPeriods: true })
    })
  }
})

describe('the inbound usage chart limits its periods by the spacing of the usage rows', () => {
  it('reads the backend bucket limit and usage row spacing', () => {
    expect(backendMaxBuckets).toBe(5000)
    expect(backendRowMinutes).toBe(USAGE_RECORD_BUCKET_MINUTES)
  })

  it('queries with the period useChartPeriodLimit lets through, counted per usage row', () => {
    const wiring = {
      limitHook:
        /const \{ allowedPeriods, periodOverride \} = useChartPeriodLimit\(\{ shortcut: selectedTime, customRange: showCustomRange \? customRange : undefined \}, selectedPeriodOverride, setPeriodOverride, \{\s*recordBucketMinutes: USAGE_RECORD_BUCKET_MINUTES,?\s*\}\)/.test(
          inboundChartSource,
        ),
      pickedPeriodState: /const \[selectedPeriodOverride, setPeriodOverride\] = useState<ChartPeriodOverride>\(CHART_PERIOD_OVERRIDE_AUTO\)/.test(inboundChartSource),
      queriesLimitedPeriod: inboundChartSource.includes('periodOverride: resolvePeriodOverride(periodOverride)'),
    }
    expect(wiring).toEqual({ limitHook: true, pickedPeriodState: true, queriesLimitedPeriod: true })
  })

  it('a week offers hour but not minute, and a long custom range offers neither', () => {
    const week = getAllowedChartPeriods({ shortcut: '1w' }, { recordBucketMinutes: USAGE_RECORD_BUCKET_MINUTES })
    expect(week).toContain(Period.hour)
    expect(week).not.toContain(Period.minute)
    const longRange = getAllowedChartPeriods({ customRange: { from: new Date(2025, 10, 1), to: new Date(2026, 10, 14) } }, { recordBucketMinutes: USAGE_RECORD_BUCKET_MINUTES })
    expect(longRange).toEqual([CHART_PERIOD_OVERRIDE_AUTO, Period.day, Period.month])
  })

  it('every period it offers for a shortcut gives a query within the backend limit', () => {
    for (const { value: shortcut } of TRAFFIC_TIME_SELECTOR_SHORTCUTS) {
      for (const offered of getAllowedChartPeriods({ shortcut }, { recordBucketMinutes: USAGE_RECORD_BUCKET_MINUTES })) {
        const range = getChartQueryRangeFromShortcut(shortcut, now, { minuteForOneHour: true, periodOverride: resolvePeriodOverride(offered as ChartPeriodOverride) })
        expect({ shortcut, offered, buckets: backendUsageBuckets(range) <= backendMaxBuckets }).toEqual({ shortcut, offered, buckets: true })
      }
    }
  })

  it('every period it offers for a custom range gives a query within the backend limit', () => {
    for (const { days, range: customRange } of customRanges) {
      for (const offered of getAllowedChartPeriods({ shortcut: '1w', customRange }, { recordBucketMinutes: USAGE_RECORD_BUCKET_MINUTES })) {
        const range = getChartQueryRangeFromDateRange(customRange, '1w', { minuteForOneHour: true, periodOverride: resolvePeriodOverride(offered as ChartPeriodOverride) })
        expect({ days, offered, buckets: backendUsageBuckets(range) <= backendMaxBuckets }).toEqual({ days, offered, buckets: true })
      }
    }
  })
})
