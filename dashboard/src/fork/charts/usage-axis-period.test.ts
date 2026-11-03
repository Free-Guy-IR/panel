import { describe, expect, it } from 'bun:test'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { Period } from '@/service/api'
import { axisPeriodOverride } from '@/fork/charts/usage-axis-period'
import { getChartXAxisInterval } from '@/utils/chart-period-utils'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')

describe('x-axis thinning follows the period actually queried', () => {
  it('a fitting override is kept for the axis', () => {
    expect(axisPeriodOverride(Period.minute, undefined)).toBe(Period.minute)
    expect(axisPeriodOverride(Period.hour, undefined)).toBe(Period.hour)
  })

  it('no override stays no override', () => {
    expect(axisPeriodOverride(undefined, undefined)).toBeUndefined()
  })

  it('a capped override is dropped so the axis thins like Auto', () => {
    expect(axisPeriodOverride(Period.minute, Period.minute)).toBeUndefined()
    expect(axisPeriodOverride(Period.hour, Period.hour)).toBeUndefined()
  })

  it('a capped All Time minute override gets the same tick interval as Auto', () => {
    const base = { dataLength: 9_800, period: Period.day, shortcut: 'all', windowWidth: 1024 }
    const auto = getChartXAxisInterval({ ...base, periodOverride: undefined })
    const capped = getChartXAxisInterval({ ...base, periodOverride: axisPeriodOverride(Period.minute, Period.minute) })
    const raw = getChartXAxisInterval({ ...base, periodOverride: Period.minute })
    expect(capped).toBe(auto)
    expect(raw).not.toBe(auto)
  })
})

describe('every chart thins its x-axis with the capped period', () => {
  const listSources = (dir: string): string[] =>
    readdirSync(dir).flatMap(name => {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) return listSources(path)
      return path.endsWith('.tsx') && !path.endsWith('.test.tsx') ? [path] : []
    })

  const callSites = listSources(join(dashboardRoot, 'src')).flatMap(file => {
    const source = readFileSync(file, 'utf8')
    const cappedFrom = source.match(/<PeriodSelector\b[^>]*\bcappedFrom=\{([^}]+)\}/)?.[1]
    return [...source.matchAll(/getChartXAxisInterval\(\{[\s\S]*?\}\),\s*\[[^\]]*\]/g)].map(([call]) => ({ file: relative(dashboardRoot, file), call, cappedFrom }))
  })

  it('finds the eight chart x-axis call sites', () => {
    expect(callSites.length).toBe(8)
  })

  for (const { file, call, cappedFrom } of callSites) {
    it(`${file} passes its cappedFrom through axisPeriodOverride`, () => {
      expect(cappedFrom).toBeDefined()
      expect(call).toContain(`periodOverride: axisPeriodOverride(resolvePeriodOverride(periodOverride), ${cappedFrom})`)
      expect(call.slice(call.lastIndexOf('['))).toContain(cappedFrom!)
    })
  }
})
