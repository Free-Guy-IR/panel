import type { Period } from '@/service/api'

export const axisPeriodOverride = (override: Period | undefined, cappedFrom: Period | undefined): Period | undefined => (cappedFrom ? undefined : override)
