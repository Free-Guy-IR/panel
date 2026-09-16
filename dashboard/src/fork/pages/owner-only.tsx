import type { ReactElement } from 'react'
import { Navigate } from 'react-router'
import type { AdminDetails } from '@/service/api'
import { useAdmin } from '@/hooks/use-admin'
import { isOwner } from '@/utils/rbac'
import { LoadingSpinner } from '@/components/common/loading-spinner'

export function OwnerOnly({ children }: { children: ReactElement }) {
  const { admin, isLoading } = useAdmin()
  if (isLoading) return <LoadingSpinner />
  if (!isOwner(admin as unknown as AdminDetails | null)) return <Navigate to="/settings" replace />
  return children
}
