import type { NodeSimple, NodeUsageStat, UserUsageStat } from '@/service/api'

export type AllNodesUsageStat = NodeUsageStat | UserUsageStat

export type AllNodesChartRow = {
  time: string
  _period_start: string
  [key: string]: string | number
}

export type AllNodesPieSlice = {
  key: string
  bytes: number
  percentage: number
}

export type AllNodesSeries = {
  rows: AllNodesChartRow[]
  seriesKeys: string[]
  totalBytes: number
  perNodeBreakdownUnavailable: boolean
  pieSlices: AllNodesPieSlice[]
}

export const AGGREGATE_STATS_KEY = '-1'
export const DELETED_NODES_STATS_KEY = '0'
export const DELETED_NODES_KEY = '__deleted_nodes__'
export const DELETED_NODES_COLOR = 'hsl(var(--muted-foreground))'

const BYTES_PER_GIGABYTE = 1024 * 1024 * 1024

const isNodeUsageStat = (point: AllNodesUsageStat): point is NodeUsageStat => 'uplink' in point && 'downlink' in point

export const getTrafficBytes = (point: AllNodesUsageStat) => {
  if ('total_traffic' in point) {
    return Number(point.total_traffic || 0)
  }
  return Number(point.uplink || 0) + Number(point.downlink || 0)
}

export const getDirectionalTraffic = (point: AllNodesUsageStat) => {
  if (isNodeUsageStat(point) && !('total_traffic' in point)) {
    return {
      uplink: Number(point.uplink || 0),
      downlink: Number(point.downlink || 0),
    }
  }

  return {
    uplink: 0,
    downlink: 0,
  }
}

export const isSeriesKey = (key: string) => key === DELETED_NODES_KEY || (key !== 'time' && !key.startsWith('_'))

export const resolveDeletedNodesLabel = (label: string, nodeNames: readonly string[]) => {
  const taken = new Set(nodeNames)
  let candidate = label
  for (let suffix = 2; taken.has(candidate); suffix += 1) {
    candidate = `${label} (${suffix})`
  }
  return candidate
}

export const relabelDeletedNodesRow = (row: AllNodesChartRow, label: string): AllNodesChartRow => {
  if (!(DELETED_NODES_KEY in row)) {
    return row
  }

  const renames = new Map([
    [DELETED_NODES_KEY, label],
    [`_uplink_${DELETED_NODES_KEY}`, `_uplink_${label}`],
    [`_downlink_${DELETED_NODES_KEY}`, `_downlink_${label}`],
  ])
  const relabelled: AllNodesChartRow = { time: row.time, _period_start: row._period_start }
  for (const [key, value] of Object.entries(row)) {
    relabelled[renames.get(key) ?? key] = value
  }
  return relabelled
}

export type AllNodesDrawnSeries = {
  key: string
  stats: AllNodesUsageStat[]
}

export const buildUsageSeries = (
  sourceStats: Record<string, AllNodesUsageStat[]>,
  series: AllNodesDrawnSeries[],
  labelFor: (periodStart: string) => string,
  deletedStats: AllNodesUsageStat[] = [],
): AllNodesSeries => {
  const statsKeys = Object.keys(sourceStats)
  if (statsKeys.length === 0) {
    return { rows: [], seriesKeys: [], totalBytes: 0, perNodeBreakdownUnavailable: false, pieSlices: [] }
  }

  const hasIndividualNodeData = statsKeys.some(key => key !== AGGREGATE_STATS_KEY)

  if (!hasIndividualNodeData && Array.isArray(sourceStats[AGGREGATE_STATS_KEY])) {
    const aggregatedStats = sourceStats[AGGREGATE_STATS_KEY]
    const totalBytes = aggregatedStats.reduce((sum, point) => sum + getTrafficBytes(point), 0)

    return { rows: [], seriesKeys: [], totalBytes, perNodeBreakdownUnavailable: true, pieSlices: [] }
  }

  const drawnSeries = [...series]
  if (deletedStats.reduce((sum, point) => sum + getTrafficBytes(point), 0) > 0) {
    drawnSeries.push({ key: DELETED_NODES_KEY, stats: deletedStats })
  }

  const allPeriods = new Set<string>()
  drawnSeries.forEach(({ stats }) => {
    stats.forEach(stat => allPeriods.add(stat.period_start))
  })

  const bytesByKey = new Map(drawnSeries.map(item => [item.key, 0]))
  const sortedPeriods = Array.from(allPeriods).sort()
  const rows = sortedPeriods.map(periodStart => {
    const row: AllNodesChartRow = {
      time: labelFor(periodStart),
      _period_start: periodStart,
    }

    drawnSeries.forEach(({ key, stats }) => {
      const periodStats = stats.find(stat => stat.period_start === periodStart)
      if (!periodStats) {
        row[key] = 0
        row[`_uplink_${key}`] = 0
        row[`_downlink_${key}`] = 0
        return
      }

      const usageBytes = getTrafficBytes(periodStats)
      const directionalTraffic = getDirectionalTraffic(periodStats)
      row[key] = usageBytes / BYTES_PER_GIGABYTE
      row[`_uplink_${key}`] = directionalTraffic.uplink
      row[`_downlink_${key}`] = directionalTraffic.downlink
      bytesByKey.set(key, (bytesByKey.get(key) ?? 0) + usageBytes)
    })

    return row
  })

  const totalBytes = Array.from(bytesByKey.values()).reduce((sum, bytes) => sum + bytes, 0)
  const toSlice = (key: string, bytes: number): AllNodesPieSlice => ({
    key,
    bytes,
    percentage: totalBytes > 0 ? (bytes * 100) / totalBytes : 0,
  })
  const liveSlices = drawnSeries
    .filter(item => item.key !== DELETED_NODES_KEY)
    .map(item => toSlice(item.key, bytesByKey.get(item.key) ?? 0))
    .filter(slice => slice.bytes > 0)
    .sort((a, b) => b.bytes - a.bytes)
  const deletedBytes = bytesByKey.get(DELETED_NODES_KEY) ?? 0
  const pieSlices = deletedBytes > 0 ? [...liveSlices, toSlice(DELETED_NODES_KEY, deletedBytes)] : liveSlices

  return {
    rows,
    seriesKeys: drawnSeries.map(item => item.key),
    totalBytes,
    perNodeBreakdownUnavailable: false,
    pieSlices,
  }
}

export const buildAllNodesSeries = (statsByNode: Record<string, AllNodesUsageStat[]>, nodeList: Pick<NodeSimple, 'id' | 'name'>[], labelFor: (periodStart: string) => string): AllNodesSeries =>
  buildUsageSeries(
    statsByNode,
    nodeList.map(node => ({ key: node.name, stats: statsByNode[String(node.id)] ?? [] })),
    labelFor,
    statsByNode[DELETED_NODES_STATS_KEY] ?? [],
  )
