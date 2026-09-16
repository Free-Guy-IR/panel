import { Suspense, type ReactElement } from 'react'
import type { RouteObject } from 'react-router'
import { LoadingSpinner } from '@/components/common/loading-spinner'
import { lazyWithChunkRecovery } from '@/utils/chunk-recovery'
import { extraRoutes, registerRoute } from '../registry'
import { OwnerOnly } from './owner-only'

const ContentFilterPage = lazyWithChunkRecovery(() => import('./content-filter'))
const ConnectionLimitSettings = lazyWithChunkRecovery(() => import('./connection-limit'))
const ConnectionLimitReview = lazyWithChunkRecovery(() => import('./connection-limit-review'))
const ConnectionLimitViolations = lazyWithChunkRecovery(() => import('./connection-limit-violations'))
const BulkL2tpPage = lazyWithChunkRecovery(() => import('./bulk-l2tp'))
const BulkMtprotoPage = lazyWithChunkRecovery(() => import('./bulk-mtproto'))

function withSpinner(element: ReactElement) {
  return <Suspense fallback={<LoadingSpinner />}>{element}</Suspense>
}

registerRoute({ path: '/settings/content-filter', element: <OwnerOnly>{withSpinner(<ContentFilterPage />)}</OwnerOnly> }, 'settings', true)
registerRoute({ path: '/settings/connection-limit', element: withSpinner(<ConnectionLimitSettings />) }, 'settings')
registerRoute({ path: '/settings/connection-limit/review', element: withSpinner(<ConnectionLimitReview />) }, 'settings')
registerRoute({ path: '/settings/connection-limit/violations', element: withSpinner(<ConnectionLimitViolations />) }, 'settings')
registerRoute({ path: '/bulk/l2tp', element: withSpinner(<BulkL2tpPage />) }, 'bulk')
registerRoute({ path: '/bulk/mtproto', element: withSpinner(<BulkMtprotoPage />) }, 'bulk')

export const forkSettingsRoutes: RouteObject[] = extraRoutes('settings')
export const forkBulkRoutes: RouteObject[] = extraRoutes('bulk')
