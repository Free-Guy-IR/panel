import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const inboundChartSource = readFileSync(join(import.meta.dir, '..', '..', 'features', 'statistics', 'components', 'inbound-usage-chart.tsx'), 'utf8')

const usesAccentPalette = (source: string) => source.includes('generateDistinctColor') || /--chart-\$\{(index|idx) \+ 1\}/.test(source)

describe('inbound usage chart series colors', () => {
  it('imports getChartSeriesColor like the all-nodes chart it was copied from', () => {
    expect(inboundChartSource.includes("import { getChartSeriesColor } from '@/utils/chart-colors'")).toBe(true)
  })

  it('colors every inbound series with getChartSeriesColor', () => {
    expect(inboundChartSource.includes('color: getChartSeriesColor(index, isDark)')).toBe(true)
  })

  it('no longer derives series colors from the accent palette', () => {
    expect(usesAccentPalette(inboundChartSource)).toBe(false)
  })
})
