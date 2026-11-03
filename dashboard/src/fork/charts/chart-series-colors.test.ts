import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const srcRoot = join(import.meta.dir, '..', '..')

type ChartColors = { getChartSeriesColor: (index: number, isDark: boolean) => string }

const loadChartColors = async (): Promise<ChartColors> => (await import(join(srcRoot, 'utils', 'chart-colors.ts'))) as ChartColors

const SERIES_COUNT = 40

const seriesColorCharts = ['components/charts/all-nodes-stacked-bar-chart.tsx', 'components/charts/user-sub-update-pie-chart.tsx', 'features/users/dialogs/usage-modal.tsx']

describe('getChartSeriesColor', () => {
  for (const isDark of [false, true]) {
    it(`gives ${SERIES_COUNT} pairwise distinct ${isDark ? 'dark' : 'light'} colors`, async () => {
      const { getChartSeriesColor } = await loadChartColors()
      const colors = Array.from({ length: SERIES_COUNT }, (_, index) => getChartSeriesColor(index, isDark))
      expect(new Set(colors).size).toBe(SERIES_COUNT)
    })
  }

  it('returns the same color for an index whatever order the indexes are asked in', async () => {
    const { getChartSeriesColor } = await loadChartColors()
    const backwards = Array.from({ length: SERIES_COUNT }, (_, offset) => SERIES_COUNT - 1 - offset).map(index => [index, getChartSeriesColor(index, true), getChartSeriesColor(index, false)] as const)
    for (const [index, dark, light] of backwards) {
      expect(getChartSeriesColor(index, true)).toBe(dark)
      expect(getChartSeriesColor(index, false)).toBe(light)
    }
  })

  it('never resolves through a CSS accent variable and pairs a separate dark shade', async () => {
    const { getChartSeriesColor } = await loadChartColors()
    for (let index = 0; index < SERIES_COUNT; index++) {
      const light = getChartSeriesColor(index, false)
      const dark = getChartSeriesColor(index, true)
      for (const color of [light, dark]) {
        expect(color).not.toContain('var(')
        expect(color).toMatch(/^(#[0-9a-f]{6}|hsl\(\d+(\.\d+)?, \d+%, \d+%\))$/)
      }
      expect(dark).not.toBe(light)
    }
  })
})

describe('series charts take their colors from getChartSeriesColor', () => {
  for (const file of seriesColorCharts) {
    it(`${file} colors series with getChartSeriesColor instead of the accent palette`, () => {
      const source = readFileSync(join(srcRoot, file), 'utf8')
      expect(source).toMatch(/import \{ getChartSeriesColor \} from '@\/utils\/chart-colors'/)
      expect(source).toContain('getChartSeriesColor(')
      expect(source).not.toContain('generateDistinctColor')
      expect(source).not.toContain('getSegmentColor')
      expect(source).not.toMatch(/--chart-\$\{(index|idx) \+ 1\}/)
    })
  }

  it('the all-nodes chart keeps its own muted color for the deleted-nodes bucket', () => {
    const source = readFileSync(join(srcRoot, 'components/charts/all-nodes-stacked-bar-chart.tsx'), 'utf8')
    expect(source).toContain('config[DELETED_NODES_KEY] = { label: deletedNodesLabel, color: DELETED_NODES_COLOR }')
  })
})
