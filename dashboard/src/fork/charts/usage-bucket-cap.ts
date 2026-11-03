import { Period } from '@/service/api'

export const USAGE_MAX_BUCKETS = 50_000

const MINUTE_MS = 60_000
const DAY_MS = 1_440 * MINUTE_MS

export const USAGE_PERIOD_LENGTH_MS: Record<Period, number> = {
  [Period.minute]: MINUTE_MS,
  [Period.hour]: 60 * MINUTE_MS,
  [Period.day]: DAY_MS,
  [Period.month]: 28 * DAY_MS,
}

type DateValue = string | Date

const toEpochMs = (value: DateValue) => (value instanceof Date ? value.getTime() : Date.parse(value))

export const usageBucketCount = (period: Period, start: DateValue, end: DateValue) => (toEpochMs(end) - toEpochMs(start)) / USAGE_PERIOD_LENGTH_MS[period]

export const exceedsUsageBucketCap = (period: Period, start: DateValue, end: DateValue) => usageBucketCount(period, start, end) > USAGE_MAX_BUCKETS

export type CappedUsagePeriod = { period: Period; cappedFrom?: Period }

export const capUsagePeriod = (requested: Period | undefined, auto: Period, start: DateValue, end: DateValue): CappedUsagePeriod => {
  if (!requested) return { period: auto }
  if (requested === auto || !exceedsUsageBucketCap(requested, start, end)) return { period: requested }
  return { period: auto, cappedFrom: requested }
}

type UsageQueryRange = { period: Period; startDate: string; endDate: string }

export const fitUsageQueryRange = <T extends UsageQueryRange>(build: (period: Period) => T, requested: Period, auto: Period): T & { cappedFrom?: Period } => {
  const range = build(requested)
  if (requested === auto || !exceedsUsageBucketCap(range.period, range.startDate, range.endDate)) return range
  return { ...build(auto), cappedFrom: requested }
}
