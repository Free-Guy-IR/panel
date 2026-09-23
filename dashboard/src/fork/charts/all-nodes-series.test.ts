import { describe, expect, it } from 'bun:test'

import en from '../locales/en.json'
import fa from '../locales/fa.json'
import ru from '../locales/ru.json'
import zh from '../locales/zh.json'
import { type AllNodesSeries, type AllNodesUsageStat, DELETED_NODES_KEY, buildAllNodesSeries, isSeriesKey, relabelDeletedNodesRow, resolveDeletedNodesLabel } from './all-nodes-series'

const GIB = 1024 * 1024 * 1024
const P1 = '2026-09-20T00:00:00Z'
const P2 = '2026-09-21T00:00:00Z'
const P3 = '2026-09-22T00:00:00Z'

const nodeList = [
  { id: 1, name: 'node1' },
  { id: 2, name: 'node2' },
]

const labelFor = (periodStart: string) => `label:${periodStart}`

const stat = (periodStart: string, uplinkGib: number, downlinkGib: number): AllNodesUsageStat => ({
  period_start: periodStart,
  uplink: uplinkGib * GIB,
  downlink: downlinkGib * GIB,
})

const drawnBytes = (series: AllNodesSeries) => series.rows.reduce((sum, row) => sum + series.seriesKeys.reduce((rowSum, key) => rowSum + Number(row[key] || 0) * GIB, 0), 0)

const pieBytes = (series: AllNodesSeries) => series.pieSlices.reduce((sum, slice) => sum + slice.bytes, 0)

const withDeletedNodes = {
  '1': [stat(P1, 1, 1), stat(P3, 2, 0)],
  '2': [stat(P2, 0, 3)],
  '0': [stat(P1, 2, 1), stat(P2, 1, 1)],
}

describe('buildAllNodesSeries with deleted-node usage', () => {
  const series = buildAllNodesSeries(withDeletedNodes, nodeList, labelFor)

  it('stacks the deleted-nodes series last after the live nodes', () => {
    expect(series.seriesKeys).toEqual(['node1', 'node2', DELETED_NODES_KEY])
    expect(series.seriesKeys[series.seriesKeys.length - 1]).toBe(DELETED_NODES_KEY)
    expect(series.perNodeBreakdownUnavailable).toBe(false)
  })

  it('carries the key 0 usage on every row', () => {
    expect(series.rows.map(row => row._period_start)).toEqual([P1, P2, P3])
    expect(series.rows.map(row => row.time)).toEqual([labelFor(P1), labelFor(P2), labelFor(P3)])
    for (const row of series.rows) {
      expect(DELETED_NODES_KEY in row).toBe(true)
    }
    expect(series.rows.map(row => row[DELETED_NODES_KEY])).toEqual([3, 2, 0])
    expect(series.rows.map(row => row[`_uplink_${DELETED_NODES_KEY}`])).toEqual([2 * GIB, 1 * GIB, 0])
    expect(series.rows.map(row => row[`_downlink_${DELETED_NODES_KEY}`])).toEqual([1 * GIB, 1 * GIB, 0])
  })

  it('includes the deleted-nodes slice in the pie', () => {
    const slice = series.pieSlices.find(item => item.key === DELETED_NODES_KEY)
    expect(slice?.bytes).toBe(5 * GIB)
    expect(series.pieSlices.map(item => item.key)).toEqual(['node1', 'node2', DELETED_NODES_KEY])
  })

  it('makes the total equal the drawn series and the pie', () => {
    expect(series.totalBytes).toBe(4 * GIB + 3 * GIB + 5 * GIB)
    expect(drawnBytes(series)).toBe(series.totalBytes)
    expect(pieBytes(series)).toBe(series.totalBytes)
    expect(series.pieSlices.reduce((sum, slice) => sum + slice.percentage, 0)).toBeCloseTo(100, 10)
  })

  it('draws no deleted-nodes series when key 0 sums to zero', () => {
    const quiet = buildAllNodesSeries({ '1': [stat(P1, 1, 0)], '2': [stat(P1, 0, 2)], '0': [stat(P1, 0, 0)] }, nodeList, labelFor)
    expect(quiet.seriesKeys).toEqual(['node1', 'node2'])
    expect(quiet.pieSlices.some(item => item.key === DELETED_NODES_KEY)).toBe(false)
    for (const row of quiet.rows) {
      expect(DELETED_NODES_KEY in row).toBe(false)
    }
    expect(quiet.totalBytes).toBe(3 * GIB)
    expect(drawnBytes(quiet)).toBe(quiet.totalBytes)
    expect(pieBytes(quiet)).toBe(quiet.totalBytes)
  })
})

describe('buildAllNodesSeries with an aggregate-only response', () => {
  it('keeps the measured total and invents no per-node split', () => {
    const threeNodes = [...nodeList, { id: 3, name: 'node3' }]
    const series = buildAllNodesSeries({ '-1': [stat(P1, 3, 1), stat(P2, 2, 0)] }, threeNodes, labelFor)
    expect(series.perNodeBreakdownUnavailable).toBe(true)
    expect(series.rows).toEqual([])
    expect(series.pieSlices).toEqual([])
    expect(series.seriesKeys).toEqual([])
    expect(series.totalBytes).toBe(6 * GIB)
  })

  it('returns an empty series for an empty response', () => {
    expect(buildAllNodesSeries({}, nodeList, labelFor)).toEqual({
      rows: [],
      seriesKeys: [],
      totalBytes: 0,
      perNodeBreakdownUnavailable: false,
      pieSlices: [],
    })
  })
})

describe('deleted-nodes label', () => {
  const overlays: Record<string, Record<string, unknown>> = { en, fa, ru, zh }

  for (const [language, overlay] of Object.entries(overlays)) {
    it(`is a non-empty string in the ${language} overlay`, () => {
      const label = overlay['statistics.deletedNodes']
      expect(typeof label).toBe('string')
      expect(String(label).trim().length).toBeGreaterThan(0)
    })
  }
})

describe('deleted-nodes presentation helpers', () => {
  it('treats node names and the deleted-nodes key as series keys', () => {
    expect(isSeriesKey('node1')).toBe(true)
    expect(isSeriesKey(DELETED_NODES_KEY)).toBe(true)
    expect(isSeriesKey('time')).toBe(false)
    expect(isSeriesKey('_period_start')).toBe(false)
    expect(isSeriesKey('_uplink_node1')).toBe(false)
    expect(isSeriesKey(`_downlink_${DELETED_NODES_KEY}`)).toBe(false)
  })

  it('keeps the label unless a live node already uses it', () => {
    expect(resolveDeletedNodesLabel('Deleted nodes', ['node1', 'node2'])).toBe('Deleted nodes')
    expect(resolveDeletedNodesLabel('Deleted nodes', ['Deleted nodes', 'Deleted nodes (2)'])).toBe('Deleted nodes (3)')
  })

  it('renames the deleted-nodes columns of a row to the label', () => {
    const [row] = buildAllNodesSeries(withDeletedNodes, nodeList, labelFor).rows
    const relabelled = relabelDeletedNodesRow(row, 'Deleted nodes')
    expect(relabelled).toEqual({
      time: labelFor(P1),
      _period_start: P1,
      node1: 2,
      _uplink_node1: 1 * GIB,
      _downlink_node1: 1 * GIB,
      node2: 0,
      _uplink_node2: 0,
      _downlink_node2: 0,
      'Deleted nodes': 3,
      '_uplink_Deleted nodes': 2 * GIB,
      '_downlink_Deleted nodes': 1 * GIB,
    })
    expect(DELETED_NODES_KEY in row).toBe(true)
  })

  it('returns a row without deleted-node usage unchanged', () => {
    const row = { time: 't', _period_start: P1, node1: 1 }
    expect(relabelDeletedNodesRow(row, 'Deleted nodes')).toBe(row)
  })
})
